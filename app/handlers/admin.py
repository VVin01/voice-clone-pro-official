from __future__ import annotations

import html
import json
import logging
from pathlib import Path
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import uuid
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.models.enums import (
    AccountCategory,
    BroadcastRecipientGroup,
    CustomVoiceRequestStatus,
    JobStatus,
    LedgerDirection,
    LedgerSource,
    PaymentStatus,
    UserStatus,
    VoiceModelStatus,
    VoiceModelType,
)
from app.models.schema import (
    AuditLog,
    BotSetting,
    CustomVoiceRequest,
    DatabaseMetadata,
    ImportedAudience,
    Job,
    MinuteBalance,
    MinuteLedgerEvent,
    PaymentRequest,
    Referral,
    ReferralReward,
    User,
    VoiceModel,
    utc_now,
)
from app.services.broadcast import (
    BroadcastResult,
    create_or_get_broadcast_campaign,
    deliver_broadcast,
    get_broadcast_recipient_ids,
)
from app.services.i18n import t
from app.services.ledger import (
    admin_adjust_balance,
    get_minute_balance,
    get_or_create_user,
    get_paginated_users,
    get_user_recent_ledger_events,
)
from app.services.models import (
    activate_model,
    disable_model,
    scan_all_models,
    set_free_default_model,
    set_model_free_permission,
    set_model_purchased_permission,
)
from app.services.payments import approve_payment_request, reject_payment_request
from app.services.custom_voice import (
    assign_model_to_request,
    get_custom_voice_request_by_id,
    get_custom_voice_requests,
    reject_custom_voice_request,
)
from app.ui.keyboards import (
    admin_adjust_cancel_kb,
    admin_adjust_preview_kb,
    admin_broadcast_audience_kb,
    admin_broadcast_preview_kb,
    admin_broadcast_prompt_kb,
    admin_cvr_detail_kb,
    admin_cvr_list_kb,
    admin_cvr_reject_presets_kb,
    admin_cvr_select_model_kb,
    admin_dashboard_kb,
    admin_job_detail_kb,
    admin_job_retry_confirm_kb,
    admin_pay_approve_confirm_kb,
    admin_pay_reject_confirm_kb,
    admin_queue_failures_kb,
    admin_queue_kb,
    admin_queue_list_kb,
    admin_setting_presets_kb,
    admin_settings_cancel_kb,
    admin_settings_interactive_kb,
    admin_tutorial_cancel_kb,
    admin_tutorial_confirm_remove_kb,
    admin_tutorial_mgmt_kb,
    admin_user_card_kb,
    admin_user_ledger_kb,
    admin_user_search_cancel_kb,
    admin_users_list_kb,
    admin_pricing_plan_edit_kb,
    admin_payment_accounts_edit_kb,
    admin_model_rename_cancel_kb,
    admin_model_sample_cancel_kb,
)
from app.services.dynamic_settings import load_dynamic_settings, set_dynamic_setting
from app.ui.helpers import safe_edit_message

logger = logging.getLogger(__name__)
admin_router = Router(name="admin")


class AdminModelSampleFSM(StatesGroup):
    waiting_for_sample = State()


class AdminCVRRejectFSM(StatesGroup):
    waiting_for_reason = State()


class AdminSettingsFSM(StatesGroup):
    waiting_for_value = State()


class AdminPriceEditFSM(StatesGroup):
    waiting_for_price = State()


class AdminModelRenameFSM(StatesGroup):
    waiting_for_name = State()


class AdminBroadcastFSM(StatesGroup):
    choosing_audience = State()
    waiting_for_message = State()
    confirming = State()


class AdminAdjustFSM(StatesGroup):
    waiting_for_amount = State()
    waiting_for_reason = State()
    confirming = State()


class AdminUserSearchFSM(StatesGroup):
    waiting_for_query = State()


class AdminTutorialFSM(StatesGroup):
    waiting_for_url = State()


async def is_authorized_admin(event: CallbackQuery | Message) -> bool:
    """Enforce strict server-side admin authorization."""
    user_id = event.from_user.id if event.from_user else None
    if not settings.is_admin(user_id):
        logger.warning("Unauthorized admin access attempt by user %s", user_id)
        if isinstance(event, CallbackQuery):
            await event.answer(t("admin_not_authorized", "en"), show_alert=True)
        else:
            await event.answer(t("admin_not_authorized", "en"))
        return False
    return True


async def _fetch_dashboard_metrics(session: AsyncSession) -> dict:
    total_users = (await session.execute(select(func.count()).select_from(User))).scalar() or 0
    free_users = (await session.execute(select(func.count()).where(User.account_category == AccountCategory.FREE.value))).scalar() or 0
    purchased_users = (await session.execute(select(func.count()).where(User.account_category == AccountCategory.PURCHASED.value))).scalar() or 0

    total_jobs = (await session.execute(select(func.count()).select_from(Job))).scalar() or 0
    queued_jobs = (await session.execute(select(func.count()).where(Job.status == JobStatus.QUEUED.value))).scalar() or 0
    running_jobs = (await session.execute(select(func.count()).where(Job.status == JobStatus.RUNNING.value))).scalar() or 0
    success_jobs = (await session.execute(select(func.count()).where(Job.status == JobStatus.SUCCESS.value))).scalar() or 0
    failed_jobs = (await session.execute(select(func.count()).where(Job.status == JobStatus.FAILED.value))).scalar() or 0

    last_failed_job = (await session.execute(
        select(Job).where(Job.status == JobStatus.FAILED.value).order_by(Job.created_at.desc()).limit(1)
    )).scalar_one_or_none()

    purchased_sec = (await session.execute(select(func.sum(MinuteBalance.purchased_seconds)))).scalar() or 0
    consumed_sec = (await session.execute(select(func.sum(MinuteBalance.consumed_seconds)))).scalar() or 0

    pending_payments = (await session.execute(select(func.count()).where(PaymentRequest.status == PaymentStatus.PENDING.value))).scalar() or 0
    active_models = (await session.execute(select(func.count()).where(VoiceModel.status == VoiceModelStatus.ACTIVE.value))).scalar() or 0
    pending_cvr = (await session.execute(select(func.count()).where(CustomVoiceRequest.status == CustomVoiceRequestStatus.PENDING.value))).scalar() or 0

    return {
        "total_users": total_users,
        "free_users": free_users,
        "purchased_users": purchased_users,
        "total_jobs": total_jobs,
        "queued_jobs": queued_jobs,
        "running_jobs": running_jobs,
        "success_jobs": success_jobs,
        "failed_jobs": failed_jobs,
        "last_failed_error": last_failed_job.error_message if last_failed_job else None,
        "purchased_minutes": round(purchased_sec / 60.0, 1),
        "consumed_minutes": round(consumed_sec / 60.0, 1),
        "pending_payments": pending_payments,
        "active_models": active_models,
        "pending_cvr": pending_cvr,
    }


async def render_admin_dashboard(
    admin_id: int,
    lang: str = "my",
    session: AsyncSession | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Compile comprehensive, data-backed dashboard summary with HTML safety."""
    if session is not None:
        data = await _fetch_dashboard_metrics(session)
    else:
        async with AsyncSessionLocal() as s:
            data = await _fetch_dashboard_metrics(s)

    failed_count = data["failed_jobs"]
    if failed_count > 0:
        safe_err = html.escape(str(data["last_failed_error"] or "Error")[:60])
        issues_text = t("admin_issues_failed", lang, count=failed_count, error=safe_err)
    else:
        issues_text = t("admin_issues_clean", lang)

    text = t(
        "admin_dashboard_status",
        lang,
        admin_title=t("admin_title", lang),
        admin_id=admin_id,
        total_users=data["total_users"],
        free_users=data["free_users"],
        purchased_users=data["purchased_users"],
        total_jobs=data["total_jobs"],
        queued_jobs=data["queued_jobs"],
        running_jobs=data["running_jobs"],
        success_jobs=data["success_jobs"],
        failed_jobs=data["failed_jobs"],
        purchased_minutes=data["purchased_minutes"],
        consumed_minutes=data["consumed_minutes"],
        pending_payments=data["pending_payments"],
        active_models=data["active_models"],
        recent_issues=issues_text,
    )
    kb = admin_dashboard_kb(
        lang=lang,
        pending_payments=data["pending_payments"],
        pending_cvr=data.get("pending_cvr", 0),
    )
    return text, kb


@admin_router.message(Command("admin"))
async def handle_admin_command(message: Message, session: AsyncSession | None = None):
    if not await is_authorized_admin(message):
        return

    telegram_id = message.from_user.id
    if session is not None:
        user, _ = await get_or_create_user(session, telegram_id)
        lang = user.language_code
        text, kb = await render_admin_dashboard(telegram_id, lang=lang, session=session)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, telegram_id)
            lang = user.language_code
            text, kb = await render_admin_dashboard(telegram_id, lang=lang, session=s)

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@admin_router.callback_query(F.data.in_({"admin:dashboard", "admin:refresh"}))
async def handle_admin_dashboard(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    telegram_id = callback.from_user.id
    if session is not None:
        user, _ = await get_or_create_user(session, telegram_id)
        lang = user.language_code
        text, kb = await render_admin_dashboard(telegram_id, lang=lang, session=session)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, telegram_id)
            lang = user.language_code
            text, kb = await render_admin_dashboard(telegram_id, lang=lang, session=s)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode="HTML",
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.warning("Failed to edit admin dashboard message: %s", e)

    if callback.data == "admin:refresh":
        await callback.answer(t("admin_alert_refreshed", lang))
    else:
        await callback.answer()


# ---------------------------------------------------------------------------
# Voice Management Handlers
# ---------------------------------------------------------------------------

@admin_router.callback_query(F.data == "admin:voices")
async def handle_admin_voices(callback: CallbackQuery):
    if not await is_authorized_admin(callback):
        return

    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        models = (await session.execute(
            select(VoiceModel).order_by(VoiceModel.type, VoiceModel.name)
        )).scalars().all()

    def badge(m: VoiceModel) -> str:
        pth_exists = Path(m.pth_path).is_file() if m.pth_path else False
        if m.status == VoiceModelStatus.ACTIVE.value and pth_exists:
            return "🟢"
        if m.status == VoiceModelStatus.READY.value and pth_exists:
            return "🟡"
        if m.status == VoiceModelStatus.DISABLED.value:
            return "⚪"
        return "🔴"

    def sample_badge(m: VoiceModel) -> str:
        if m.sample_audio_path and Path(m.sample_audio_path).is_file():
            return " 🎧"
        return ""

    buttons = [
        [
            InlineKeyboardButton(
                text=f"{badge(m)} {m.name} ({m.status}){' ⭐' if m.is_default_free else ''}{sample_badge(m)}",
                callback_data=f"admin:model:{m.id}",
            )
        ]
        for m in models
    ]
    buttons.append([
        InlineKeyboardButton(text="🎧 Voice Samples မန်နေဂျာ", callback_data="admin:samples_mgr"),
        InlineKeyboardButton(text=t("admin_btn_scan_models", lang), callback_data="admin:model_scan"),
    ])
    buttons.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="admin:dashboard")])

    text = t("admin_voice_mgmt_title", lang)
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data == "admin:samples_mgr")
async def handle_admin_samples_mgr(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        models = (await session.execute(
            select(VoiceModel).order_by(VoiceModel.type, VoiceModel.name)
        )).scalars().all()
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            models = (await s.execute(
                select(VoiceModel).order_by(VoiceModel.type, VoiceModel.name)
            )).scalars().all()

    buttons = []
    for m in models:
        has_sample = bool(m.sample_audio_path and Path(m.sample_audio_path).is_file())
        status_icon = "🟢" if has_sample else "⚪"
        sample_tag = "Ready 🎧" if has_sample else "မရှိသေးပါ"
        label = f"{status_icon} {m.name} ({sample_tag})"
        buttons.append([
            InlineKeyboardButton(text=label, callback_data=f"admin:mod_sample_menu:{m.id}")
        ])

    buttons.append([
        InlineKeyboardButton(text="🎙️ မော်ဒယ်လ်များ စာရင်း", callback_data="admin:voices"),
        InlineKeyboardButton(text="🔙 Admin Dashboard", callback_data="admin:dashboard"),
    ])

    text = (
        "🎧 <b>Voice Model Samples မန်နေဂျာ (Admin Dashboard)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "မော်ဒယ်တစ်ခုချင်းစီအတွက် Sample အသံဖိုင် စစ်ဆေးခြင်း၊ နားဆင်ခြင်း နှင့် တိုက်ရိုက် Upload တင်ရန် သက်ဆိုင်ရာ Model ကို နှိပ်ပါ:\n\n"
        "• 🟢 = Sample အသံဖိုင် ရှိပြီးပါပြီ\n"
        "• ⚪ = Sample အသံဖိုင် မရှိသေးပါ (Upload တင်ရန် လိုအပ်သည်)\n"
    )
    await safe_edit_message(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:mod_sample_menu:"))
async def handle_admin_model_sample_menu(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    try:
        model_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Invalid model", show_alert=True)
        return

    if session is not None:
        model = await session.get(VoiceModel, model_id)
    else:
        async with AsyncSessionLocal() as s:
            model = await s.get(VoiceModel, model_id)

    if not model:
        await callback.answer("Model not found", show_alert=True)
        return

    sample_exists = bool(model.sample_audio_path and Path(model.sample_audio_path).is_file())
    status_str = "🟢 အသံဖိုင် ရှိပြီးပါပြီ" if sample_exists else "⚪ အသံဖိုင် မရှိသေးပါ"
    path_str = html.escape(str(model.sample_audio_path or "None"))

    text = (
        f"🎧 <b>Voice Sample စီမံခန့်ခွဲမှု: {html.escape(model.name)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"• အခြေအနေ: <b>{status_str}</b>\n"
        f"• ဖိုင်လမ်းကြောင်း: <code>{path_str}</code>\n\n"
        f"<i>အောက်ပါ ခလုတ်များဖြင့် Sample အသံဖိုင် စမ်းသပ်နားထောင်ခြင်း သို့မဟုတ် အသံဖိုင်အသစ် တင်ခြင်း ပြုလုပ်နိုင်ပါသည်:</i>"
    )

    buttons = []
    ctrl_row = []
    if sample_exists:
        ctrl_row.append(InlineKeyboardButton(text="▶️ နားထောင်မည် (Play)", callback_data=f"admin:mod_sample_play:{model.id}"))
    ctrl_row.append(
        InlineKeyboardButton(
            text="🎵 Sample အသံတင်မည်" if not sample_exists else "🔄 အသံအသစ်လဲမည်",
            callback_data=f"admin:mod_sample_up:{model.id}",
        )
    )
    if sample_exists:
        ctrl_row.append(InlineKeyboardButton(text="🗑️ ဖျက်မည်", callback_data=f"admin:mod_sample_del:{model.id}"))
    buttons.append(ctrl_row)

    buttons.append([
        InlineKeyboardButton(text="🔍 Model Detail ကြည့်ရန်", callback_data=f"admin:model:{model.id}"),
        InlineKeyboardButton(text="🔙 Samples စာရင်းသို့", callback_data="admin:samples_mgr"),
    ])

    await safe_edit_message(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data == "admin:model_scan")
async def handle_admin_model_scan(callback: CallbackQuery):
    if not await is_authorized_admin(callback):
        return

    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        scanned = await scan_all_models(session)
        await session.commit()

    await callback.answer(t("admin_alert_scanned", lang, count=len(scanned)), show_alert=False)
    await handle_admin_voices(callback)


async def render_admin_model_detail(
    callback: CallbackQuery,
    model_id: int,
    session: AsyncSession | None = None,
    answer_callback: bool = True,
):
    if not await is_authorized_admin(callback):
        return

    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        model = await session.get(VoiceModel, model_id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            model = await s.get(VoiceModel, model_id)

    if not model:
        await callback.answer(t("admin_alert_model_not_found", lang), show_alert=True)
        return

    pth_ok = Path(model.pth_path).is_file() if model.pth_path else False
    idx_ok = Path(model.index_path).is_file() if model.index_path else False

    safe_pth_path = html.escape(str(model.pth_path or ""))
    free_perm_icon = "✅ Allowed" if model.allowed_for_free else "❌ Disallowed"
    purch_perm_icon = "✅ Allowed" if model.allowed_for_purchased else "❌ Disallowed"

    sample_file = getattr(model, "sample_audio_path", None)
    sample_ok = bool(
        sample_file
        and (
            Path(sample_file).is_file()
            or sample_file.startswith("AgAC")
            or sample_file.startswith("CQAC")
            or (len(sample_file) > 30 and not sample_file.startswith("/"))
        )
    )
    sample_badge = "🟢 Ready" if sample_ok else "⚪ Not uploaded"

    base_detail = t(
        "admin_model_detail",
        lang,
        name=model.name,
        status=model.status,
        type=model.type,
        free_default="Yes ⭐" if model.is_default_free else "No",
        pth_status="🟢 Ready" if pth_ok else "🔴 Missing",
        idx_status="🟢 Ready" if idx_ok else "⚪ Optional/Missing",
        pth_path=safe_pth_path,
    )
    text = (
        f"{base_detail}\n"
        f"• Free Account Access: <b>{free_perm_icon}</b>\n"
        f"• Purchased Account Access: <b>{purch_perm_icon}</b>\n"
        f"• Voice Sample: <b>{sample_badge}</b>\n"
    )

    buttons = []
    if model.status == VoiceModelStatus.ACTIVE.value:
        buttons.append([InlineKeyboardButton(text=t("admin_btn_disable", lang), callback_data=f"admin:mod_toggle:{model.id}:disable")])
        if model.type == VoiceModelType.SYSTEM.value:
            free_label = t("admin_btn_unset_free", lang) if model.is_default_free else t("admin_btn_set_free", lang)
            buttons.append([InlineKeyboardButton(text=free_label, callback_data=f"admin:mod_free:{model.id}")])
    else:
        buttons.append([InlineKeyboardButton(text=t("admin_btn_activate", lang), callback_data=f"admin:mod_toggle:{model.id}:activate")])

    # Tier permission toggle buttons
    free_toggle_btn = f"Free: {'Revoke ❌' if model.allowed_for_free else 'Allow ✅'}"
    purch_toggle_btn = f"Purchased: {'Revoke ❌' if model.allowed_for_purchased else 'Allow ✅'}"
    buttons.append([
        InlineKeyboardButton(text=free_toggle_btn, callback_data=f"admin:mod_perm:{model.id}:free"),
        InlineKeyboardButton(text=purch_toggle_btn, callback_data=f"admin:mod_perm:{model.id}:purchased"),
    ])

    # Voice sample audio controls
    sample_row = []
    if sample_ok:
        sample_row.append(InlineKeyboardButton(text="▶️ Play Sample", callback_data=f"admin:mod_sample_play:{model.id}"))
    sample_row.append(
        InlineKeyboardButton(
            text="🎵 Upload Sample" if not sample_ok else "🔄 Replace Sample",
            callback_data=f"admin:mod_sample_up:{model.id}",
        )
    )
    if sample_ok:
        sample_row.append(InlineKeyboardButton(text="🗑️ Remove Sample", callback_data=f"admin:mod_sample_del:{model.id}"))
    buttons.append(sample_row)

    buttons.append([
        InlineKeyboardButton(text="✏️ Rename Model", callback_data=f"admin:mod_rename:{model.id}"),
        InlineKeyboardButton(text=t("admin_btn_back_to_models", lang), callback_data="admin:voices"),
    ])

    try:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    if answer_callback:
        try:
            await callback.answer()
        except Exception:
            pass


@admin_router.callback_query(F.data.startswith("admin:model:"))
async def handle_admin_model_detail(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    try:
        model_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Malformed model callback", show_alert=True)
        return

    await render_admin_model_detail(callback, model_id=model_id, session=session)


@admin_router.callback_query(F.data.startswith("admin:mod_toggle:"))
async def handle_admin_model_toggle(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    parts = callback.data.split(":")
    try:
        model_id, action = int(parts[2]), parts[3]
    except (IndexError, ValueError):
        await callback.answer("Malformed toggle callback", show_alert=True)
        return

    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        if action == "activate":
            ok, msg = await activate_model(session, model_id, admin_id=callback.from_user.id)
        else:
            ok, msg = await disable_model(session, model_id, admin_id=callback.from_user.id)
        await session.commit()
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            if action == "activate":
                ok, msg = await activate_model(s, model_id, admin_id=callback.from_user.id)
            else:
                ok, msg = await disable_model(s, model_id, admin_id=callback.from_user.id)
            await s.commit()

    await callback.answer(t("admin_alert_model_toggled", lang, action=action, msg=msg), show_alert=False)
    await render_admin_model_detail(callback, model_id=model_id, session=session)


@admin_router.callback_query(F.data.startswith("admin:mod_free:"))
async def handle_admin_model_free(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    try:
        model_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Malformed model free callback", show_alert=True)
        return

    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        ok, msg = await set_free_default_model(session, model_id, admin_id=callback.from_user.id)
        await session.commit()
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            ok, msg = await set_free_default_model(s, model_id, admin_id=callback.from_user.id)
            await s.commit()

    await callback.answer(t("admin_alert_free_updated", lang, msg=msg), show_alert=False)
    await render_admin_model_detail(callback, model_id=model_id, session=session)



@admin_router.callback_query(F.data.startswith("admin:mod_perm:"))
async def handle_admin_model_perm_toggle(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    parts = callback.data.split(":")
    try:
        model_id = int(parts[2])
        tier = parts[3]
    except (IndexError, ValueError):
        await callback.answer("Malformed permission toggle callback", show_alert=True)
        return

    if session is not None:
        model = await session.get(VoiceModel, model_id)
        if model:
            if tier == "free":
                await set_model_free_permission(session, model_id, not model.allowed_for_free, callback.from_user.id)
            elif tier == "purchased":
                await set_model_purchased_permission(session, model_id, not model.allowed_for_purchased, callback.from_user.id)
            await session.commit()
    else:
        async with AsyncSessionLocal() as s:
            model = await s.get(VoiceModel, model_id)
            if model:
                if tier == "free":
                    await set_model_free_permission(s, model_id, not model.allowed_for_free, callback.from_user.id)
                elif tier == "purchased":
                    await set_model_purchased_permission(s, model_id, not model.allowed_for_purchased, callback.from_user.id)
                await s.commit()

    await callback.answer("Model permissions updated.", show_alert=False)
    await render_admin_model_detail(callback, model_id=model_id, session=session)


@admin_router.callback_query(F.data.startswith("admin:mod_rename:"))
async def handle_admin_model_rename_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    model_id = int(callback.data.split(":")[2])
    if session is not None:
        model = await session.get(VoiceModel, model_id)
    else:
        async with AsyncSessionLocal() as s:
            model = await s.get(VoiceModel, model_id)

    if not model:
        await callback.answer("Model not found", show_alert=True)
        return

    await state.set_state(AdminModelRenameFSM.waiting_for_name)
    await state.update_data(model_id=model_id)

    text = (
        f"✏️ <b>Rename Voice Model</b>\n\n"
        f"• Current Name: <b>{model.name}</b>\n"
        f"• Model ID: <code>{model.id}</code>\n\n"
        "Please send the new display name as a text message below:"
    )
    await callback.message.edit_text(text, reply_markup=admin_model_rename_cancel_kb(model_id), parse_mode="HTML")
    await callback.answer()


@admin_router.message(AdminModelRenameFSM.waiting_for_name)
async def handle_admin_model_rename_receive(message: Message, state: FSMContext, session: AsyncSession | None = None):
    if not settings.is_admin(message.from_user.id):
        return
    data = await state.get_data()
    model_id = data.get("model_id")
    new_name = (message.text or "").strip()

    if not new_name or len(new_name) < 2 or len(new_name) > 60:
        await message.answer("❌ Invalid name. Please enter a name between 2 and 60 characters:")
        return

    from app.services.models import rename_voice_model
    if session is not None:
        ok, model, msg = await rename_voice_model(session, model_id, new_name, admin_id=message.from_user.id)
        await session.commit()
    else:
        async with AsyncSessionLocal() as s:
            ok, model, msg = await rename_voice_model(s, model_id, new_name, admin_id=message.from_user.id)
            await s.commit()

    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎙️ Back to Model", callback_data=f"admin:model:{model_id}")],
        [InlineKeyboardButton(text="🎙️ All Voice Models", callback_data="admin:voices")],
    ])
    await message.answer(f"✅ Voice model renamed successfully to: <b>{new_name}</b>", reply_markup=kb, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Admin Voice Model Sample Handlers
# ---------------------------------------------------------------------------

@admin_router.callback_query(F.data.startswith("admin:mod_sample_play:"))
async def handle_admin_model_sample_play(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    model_id = int(callback.data.split(":")[2])
    if session is not None:
        model = await session.get(VoiceModel, model_id)
    else:
        async with AsyncSessionLocal() as s:
            model = await s.get(VoiceModel, model_id)

    if not model or not model.sample_audio_path:
        await callback.answer("No sample audio available for this model", show_alert=True)
        return

    sample_path = model.sample_audio_path
    caption = f"🎙️ <b>{html.escape(model.name)}</b> Sample Audio (Admin Preview)"
    try:
        if Path(sample_path).is_file():
            p = Path(sample_path)
            audio_input = FSInputFile(str(p), filename=f"{model.name}_sample{p.suffix}")
            if p.suffix.lower() in (".ogg", ".oga"):
                await callback.bot.send_voice(
                    chat_id=callback.from_user.id,
                    voice=audio_input,
                    caption=caption,
                    parse_mode="HTML",
                )
            else:
                await callback.bot.send_audio(
                    chat_id=callback.from_user.id,
                    audio=audio_input,
                    caption=caption,
                    title=f"{model.name} Sample",
                    performer="Voice Clone Pro",
                    parse_mode="HTML",
                )
        else:
            await callback.bot.send_audio(
                chat_id=callback.from_user.id,
                audio=sample_path,
                caption=caption,
                title=f"{model.name} Sample",
                performer="Voice Clone Pro",
                parse_mode="HTML",
            )
        await callback.answer("Sample audio sent!")
    except Exception as exc:
        logger.error("Failed to send sample audio to admin: %s", exc)
        await callback.answer(f"Error: {exc}", show_alert=True)


@admin_router.callback_query(F.data.startswith("admin:mod_sample_up:"))
async def handle_admin_model_sample_upload_prompt(callback: CallbackQuery, state: FSMContext, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    model_id = int(callback.data.split(":")[2])
    if session is not None:
        model = await session.get(VoiceModel, model_id)
    else:
        async with AsyncSessionLocal() as s:
            model = await s.get(VoiceModel, model_id)

    if not model:
        await callback.answer("Model not found", show_alert=True)
        return

    await state.set_state(AdminModelSampleFSM.waiting_for_sample)
    await state.update_data(model_id=model_id)

    prompt = t("admin_prompt_upload_sample", "my", name=model.name)
    kb = admin_model_sample_cancel_kb(model_id, lang="my")
    await safe_edit_message(callback, prompt, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.message(AdminModelSampleFSM.waiting_for_sample)
async def handle_admin_model_sample_received(message: Message, state: FSMContext, session: AsyncSession | None = None):
    if not settings.is_admin(message.from_user.id):
        return

    data = await state.get_data()
    model_id = data.get("model_id")
    if not model_id:
        await state.clear()
        await message.answer("Error: model ID lost.")
        return

    # Check voice, audio, or document
    file_id = None
    ext = ".mp3"
    if message.voice:
        file_id = message.voice.file_id
        ext = ".ogg"
    elif message.audio:
        file_id = message.audio.file_id
        if message.audio.file_name and "." in message.audio.file_name:
            ext = Path(message.audio.file_name).suffix
    elif message.document and (message.document.mime_type or "").startswith("audio"):
        file_id = message.document.file_id
        if message.document.file_name and "." in message.document.file_name:
            ext = Path(message.document.file_name).suffix

    if not file_id:
        await message.answer("❌ ကျေးဇူးပြု၍ Audio file (MP3/WAV) သို့မဟုတ် Voice message ပေးပို့ပေးပါ:")
        return

    if session is not None:
        model = await session.get(VoiceModel, model_id)
    else:
        async with AsyncSessionLocal() as s:
            model = await s.get(VoiceModel, model_id)

    if not model:
        await state.clear()
        await message.answer("❌ Model not found in database.")
        return

    # Save into model's directory
    target_dir = Path(model.model_dir) if model.model_dir and Path(model.model_dir).is_dir() else (settings.storage_root / "models" / "system" / model.slug)
    target_dir.mkdir(parents=True, exist_ok=True)
    sample_dest = target_dir / f"sample{ext}"

    tg_file = await message.bot.get_file(file_id)
    await message.bot.download_file(tg_file.file_path, destination=sample_dest)

    # Save to database
    if session is not None:
        model.sample_audio_path = str(sample_dest)
        await session.commit()
    else:
        async with AsyncSessionLocal() as s:
            m = await s.get(VoiceModel, model_id)
            if m:
                m.sample_audio_path = str(sample_dest)
                await s.commit()

    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ နားထောင်မည် (Play Sample)", callback_data=f"admin:mod_sample_play:{model_id}")],
        [
            InlineKeyboardButton(text="🎧 Samples မန်နေဂျာ", callback_data="admin:samples_mgr"),
            InlineKeyboardButton(text="🎙️ Model အသေးစိတ်", callback_data=f"admin:model:{model_id}"),
        ],
    ])
    await message.answer(
        t("admin_sample_uploaded_success", "my", name=model.name),
        reply_markup=kb,
        parse_mode="HTML",
    )


@admin_router.callback_query(F.data.startswith("admin:mod_sample_del:"))
async def handle_admin_model_sample_delete(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    model_id = int(callback.data.split(":")[2])
    model_name = "Model"
    if session is not None:
        model = await session.get(VoiceModel, model_id)
        if model:
            model_name = model.name
            model.sample_audio_path = None
            await session.commit()
    else:
        async with AsyncSessionLocal() as s:
            model = await s.get(VoiceModel, model_id)
            if model:
                model_name = model.name
                model.sample_audio_path = None
                await s.commit()

    await callback.answer(t("admin_sample_deleted_success", "my", name=model_name), show_alert=True)
    await render_admin_model_detail(callback, model_id=model_id, session=session, answer_callback=False)



# ---------------------------------------------------------------------------
# Payment Management Handlers
# ---------------------------------------------------------------------------

@admin_router.callback_query(F.data == "admin:payments")
async def handle_admin_payments(callback: CallbackQuery):
    if not await is_authorized_admin(callback):
        return

    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        stmt = select(PaymentRequest).where(PaymentRequest.status == PaymentStatus.PENDING.value).order_by(PaymentRequest.created_at)
        pending = list((await session.execute(stmt)).scalars().all())

    if not pending:
        text = t("admin_payments_no_pending", lang)
        buttons = [
            [InlineKeyboardButton(text=t("admin_btn_view_pricing", lang), callback_data="admin:pricing")],
            [InlineKeyboardButton(text=t("btn_back", lang), callback_data="admin:dashboard")],
        ]
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
        await callback.answer()
        return

    text = t("admin_payments_pending_title", lang, count=len(pending))
    buttons = [
        [
            InlineKeyboardButton(
                text=f"#{p.id} · User {p.user_id} · {p.minutes}m ({p.amount_mmk:,} Ks)",
                callback_data=f"admin:pay_detail:{p.id}",
            )
        ]
        for p in pending
    ]
    buttons.append([InlineKeyboardButton(text=t("admin_btn_view_pricing", lang), callback_data="admin:pricing")])
    buttons.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="admin:dashboard")])

    await safe_edit_message(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("admin:pay_detail:"))
async def handle_admin_pay_detail(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    pay_id = callback.data.split(":")[2]
    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        payment = await session.get(PaymentRequest, pay_id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            payment = await s.get(PaymentRequest, pay_id)

    if not payment:
        await callback.answer(t("admin_alert_payment_not_found", lang), show_alert=True)
        return

    text = t(
        "admin_payment_detail",
        lang,
        pay_id=payment.id,
        user_id=payment.user_id,
        package_code=payment.package_code,
        minutes=payment.minutes,
        amount_mmk=payment.amount_mmk,
        status=payment.status,
        created_at=payment.created_at,
    )

    buttons = []
    if payment.receipt_file_id:
        buttons.append([InlineKeyboardButton(text=t("admin_btn_view_receipt", lang), callback_data=f"admin:pay_receipt:{payment.id}")])
    if payment.status == PaymentStatus.PENDING.value:
        buttons.append([
            InlineKeyboardButton(text=t("btn_admin_pay_approve_credit", lang), callback_data=f"admin:pay_approve:{payment.id}"),
            InlineKeyboardButton(text=t("btn_admin_pay_reject", lang), callback_data=f"admin:pay_reject:{payment.id}"),
        ])
    buttons.append([InlineKeyboardButton(text=t("admin_btn_back_to_payments", lang), callback_data="admin:payments")])

    await safe_edit_message(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("admin:pay_receipt:"))
async def handle_admin_pay_receipt(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    pay_id = callback.data.split(":")[2]
    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        payment = await session.get(PaymentRequest, pay_id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            payment = await s.get(PaymentRequest, pay_id)

    if not payment or not payment.receipt_file_id:
        await callback.answer(t("admin_alert_receipt_not_found", lang), show_alert=True)
        return

    try:
        await callback.message.answer_photo(
            photo=payment.receipt_file_id,
            caption=f"🧾 Payment #{payment.id} receipt (User {payment.user_id})",
        )
        await callback.answer()
    except Exception as e:
        logger.warning("Could not send receipt photo: %s", e)
        await callback.answer(f"Receipt ID: {payment.receipt_file_id}", show_alert=True)


@admin_router.callback_query(F.data.startswith("admin:pay_approve:"))
async def handle_admin_pay_approve_prompt(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    pay_id = callback.data.split(":")[2]
    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        payment = await session.get(PaymentRequest, pay_id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            payment = await s.get(PaymentRequest, pay_id)

    if not payment:
        await callback.answer(t("admin_alert_payment_not_found", lang), show_alert=True)
        return

    if payment.status != PaymentStatus.PENDING.value:
        await callback.answer(f"Payment is already {payment.status}!", show_alert=True)
        return

    confirm_text = (
        f"⚠️ <b>Confirm Payment Approval</b>\n\n"
        f"• Payment ID: <code>{payment.id}</code>\n"
        f"• Target User: <code>{payment.user_id}</code>\n"
        f"• Package: <b>{payment.package_code}</b> ({payment.minutes} minutes)\n"
        f"• Amount: <b>{payment.amount_mmk:,} MMK</b>\n\n"
        f"<i>Are you sure you want to approve this payment and credit {payment.minutes} minutes?</i>"
    )
    kb = admin_pay_approve_confirm_kb(payment.id, lang=lang)
    await safe_edit_message(callback, confirm_text, reply_markup=kb, parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("admin:pay_approve_confirm:"))
async def handle_admin_pay_approve_confirm(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    pay_id = callback.data.split(":")[2]
    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        ok, payment, msg = await approve_payment_request(session, pay_id, admin_id=callback.from_user.id)
        if ok and payment:
            await session.commit()
            try:
                target_user = await session.get(User, payment.user_id)
                target_lang = target_user.language_code if target_user else "my"
                await callback.bot.send_message(
                    chat_id=payment.user_id,
                    text=t("payment_approved_user_notify", target_lang, minutes=payment.minutes),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("Failed to notify user %s of payment approval: %s", payment.user_id, e)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            ok, payment, msg = await approve_payment_request(s, pay_id, admin_id=callback.from_user.id)
            if ok and payment:
                await s.commit()
                try:
                    target_user = await s.get(User, payment.user_id)
                    target_lang = target_user.language_code if target_user else "my"
                    await callback.bot.send_message(
                        chat_id=payment.user_id,
                        text=t("payment_approved_user_notify", target_lang, minutes=payment.minutes),
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.warning("Failed to notify user %s of payment approval: %s", payment.user_id, e)

    if not ok or not payment:
        await callback.answer(f"Failed to approve: {msg}", show_alert=True)
        return

    approved_text = (
        f"✅ <b>Payment Approved (#{payment.id})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• User ID: <code>{payment.user_id}</code>\n"
        f"• Package: <b>{payment.package_code}</b> ({payment.minutes} mins)\n"
        f"• Amount: <b>{payment.amount_mmk:,} MMK</b>\n"
        f"• Status: 🟢 <b>APPROVED</b>\n"
        f"• Approved by Admin: <code>{callback.from_user.id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Minutes credited to user account successfully!</i>"
    )
    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Payments Dashboard", callback_data="admin:payments")],
            [InlineKeyboardButton(text="🛠️ Admin Dashboard", callback_data="admin:dashboard")],
        ]
    )
    await safe_edit_message(callback, approved_text, reply_markup=back_kb, parse_mode="HTML")
    try:
        await callback.answer(t("admin_alert_payment_approved", lang, msg=msg), show_alert=False)
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("admin:pay_reject:"))
async def handle_admin_pay_reject_prompt(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    pay_id = callback.data.split(":")[2]
    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        payment = await session.get(PaymentRequest, pay_id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            payment = await s.get(PaymentRequest, pay_id)

    if not payment:
        await callback.answer(t("admin_alert_payment_not_found", lang), show_alert=True)
        return

    if payment.status != PaymentStatus.PENDING.value:
        await callback.answer(f"Payment is already {payment.status}!", show_alert=True)
        return

    confirm_text = (
        f"⚠️ <b>Confirm Payment Rejection</b>\n\n"
        f"• Payment ID: <code>{payment.id}</code>\n"
        f"• Target User: <code>{payment.user_id}</code>\n"
        f"• Package: <b>{payment.package_code}</b> ({payment.minutes} minutes)\n"
        f"• Amount: <b>{payment.amount_mmk:,} MMK</b>\n\n"
        f"<i>Are you sure you want to reject this payment request? No minutes will be credited.</i>"
    )
    kb = admin_pay_reject_confirm_kb(payment.id, lang=lang)
    await safe_edit_message(callback, confirm_text, reply_markup=kb, parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("admin:pay_reject_confirm:"))
async def handle_admin_pay_reject_confirm(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    pay_id = callback.data.split(":")[2]
    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        ok, payment, msg = await reject_payment_request(session, pay_id, admin_id=callback.from_user.id)
        if ok and payment:
            await session.commit()
            try:
                target_user = await session.get(User, payment.user_id)
                target_lang = target_user.language_code if target_user else "my"
                safe_reason = html.escape(str(payment.rejection_reason or "Unverified"))
                await callback.bot.send_message(
                    chat_id=payment.user_id,
                    text=t("payment_rejected_user_notify", target_lang, pay_id=payment.id, reason=safe_reason),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("Failed to notify user %s of payment rejection: %s", payment.user_id, e)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            ok, payment, msg = await reject_payment_request(s, pay_id, admin_id=callback.from_user.id)
            if ok and payment:
                await s.commit()
                try:
                    target_user = await s.get(User, payment.user_id)
                    target_lang = target_user.language_code if target_user else "my"
                    safe_reason = html.escape(str(payment.rejection_reason or "Unverified"))
                    await callback.bot.send_message(
                        chat_id=payment.user_id,
                        text=t("payment_rejected_user_notify", target_lang, pay_id=payment.id, reason=safe_reason),
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.warning("Failed to notify user %s of payment rejection: %s", payment.user_id, e)

    if not ok or not payment:
        await callback.answer(f"Failed to reject: {msg}", show_alert=True)
        return

    rejected_text = (
        f"❌ <b>Payment Rejected (#{payment.id})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• User ID: <code>{payment.user_id}</code>\n"
        f"• Package: <b>{payment.package_code}</b> ({payment.minutes} mins)\n"
        f"• Amount: <b>{payment.amount_mmk:,} MMK</b>\n"
        f"• Status: 🔴 <b>REJECTED</b>\n"
        f"• Rejected by Admin: <code>{callback.from_user.id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>No minutes were credited. User was notified.</i>"
    )
    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Payments Dashboard", callback_data="admin:payments")],
            [InlineKeyboardButton(text="🛠️ Admin Dashboard", callback_data="admin:dashboard")],
        ]
    )
    await safe_edit_message(callback, rejected_text, reply_markup=back_kb, parse_mode="HTML")
    try:
        await callback.answer(t("admin_alert_payment_rejected", lang, msg=msg), show_alert=False)
    except Exception:
        pass


# Backward compatibility aliases
handle_admin_pay_reject = handle_admin_pay_reject_confirm
handle_admin_pay_approve = handle_admin_pay_approve_confirm



# ---------------------------------------------------------------------------
# Queue Monitor & Users Handlers
# ---------------------------------------------------------------------------

NON_RETRYABLE_KEYWORDS = [
    "INSUFFICIENT_BALANCE",
    "MODEL_NOT_AUTHORIZED",
    "TEXT_EXCEEDS_",
    "DAILY_JOB_LIMIT_EXCEEDED",
    "CONCURRENT_JOB_LIMIT_EXCEEDED",
    "BLOCKED",
    "USER_SUSPENDED",
    "INVALID_INPUT",
]


def evaluate_job_retry_eligibility(job: Job, user: User | None) -> tuple[bool, str, str]:
    """Classify failure error and evaluate retry eligibility."""
    if job.status != JobStatus.FAILED.value:
        return False, "STATE_MISMATCH", f"Job is currently in '{job.status}' status (must be FAILED)."

    if not user or user.status != UserStatus.ACTIVE.value:
        return False, "USER_INACTIVE", "Target user account is suspended or blocked."

    err = str(job.error_message or "").upper()
    for kw in NON_RETRYABLE_KEYWORDS:
        if kw in err:
            return False, "POLICY_USER_ERROR", f"Non-retryable policy or user error ({kw})."

    return True, "SYSTEM_FAILURE", "Transient system or infrastructure failure (eligible for retry)."


@admin_router.callback_query(F.data == "admin:queue")
async def handle_admin_queue(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    async def _fetch_queue_data(s: AsyncSession):
        user, _ = await get_or_create_user(s, callback.from_user.id)
        lang = user.language_code
        queued = (await s.execute(select(func.count()).where(Job.status == JobStatus.QUEUED.value))).scalar() or 0
        running = (await s.execute(select(func.count()).where(Job.status == JobStatus.RUNNING.value))).scalar() or 0
        success = (await s.execute(select(func.count()).where(Job.status == JobStatus.SUCCESS.value))).scalar() or 0
        failed = (await s.execute(select(func.count()).where(Job.status == JobStatus.FAILED.value))).scalar() or 0
        cancelled = (await s.execute(select(func.count()).where(Job.status == JobStatus.CANCELLED.value))).scalar() or 0

        # Fetch recent 6 jobs across all statuses
        recent_jobs = list((await s.execute(
            select(Job).order_by(Job.created_at.desc()).limit(6)
        )).scalars().all())

        user_ids = {j.user_id for j in recent_jobs}
        model_ids = {j.model_id for j in recent_jobs if j.model_id}

        users_map = {}
        if user_ids:
            u_res = (await s.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
            users_map = {u.id: u for u in u_res}

        models_map = {}
        if model_ids:
            m_res = (await s.execute(select(VoiceModel).where(VoiceModel.id.in_(model_ids)))).scalars().all()
            models_map = {m.id: m for m in m_res}

        return lang, queued, running, success, failed, cancelled, recent_jobs, users_map, models_map

    if session is not None:
        lang, queued, running, success, failed, cancelled, recent_jobs, users_map, models_map = await _fetch_queue_data(session)
    else:
        async with AsyncSessionLocal() as s:
            lang, queued, running, success, failed, cancelled, recent_jobs, users_map, models_map = await _fetch_queue_data(s)

    base_text = t("admin_queue_monitor", lang, queued=queued, running=running, success=success, failed=failed, cancelled=cancelled)

    activity_lines = []
    if recent_jobs:
        activity_lines.append(t("admin_queue_recent_activity", lang))
        for j in recent_jobs:
            u = users_map.get(j.user_id)
            if u and u.username:
                user_label = f"@{html.escape(u.username)}"
            elif u and u.first_name:
                user_label = f"{html.escape(u.first_name)} (<code>{j.user_id}</code>)"
            else:
                user_label = f"<code>{j.user_id}</code>"

            m = models_map.get(j.model_id) if j.model_id else None
            model_name = html.escape(m.name if m else (j.edge_voice or "Custom"))

            st = str(j.status).upper()
            if st == JobStatus.SUCCESS.value:
                st_icon = "✅ အောင်မြင်" if lang == "my" else "✅ Success"
            elif st == JobStatus.RUNNING.value:
                st_icon = "⚙️ လုပ်ဆောင်ဆဲ" if lang == "my" else "⚙️ Running"
            elif st == JobStatus.QUEUED.value:
                st_icon = "⏳ စောင့်ဆိုင်းဆဲ" if lang == "my" else "⏳ Queued"
            elif st == JobStatus.FAILED.value:
                st_icon = "❌ မအောင်မြင်" if lang == "my" else "❌ Failed"
            else:
                st_icon = f"🚫 {st}"

            dur = f"{j.seconds_consumed}s" if j.seconds_consumed > 0 else f"{j.seconds_estimated}s"
            ts_str = j.created_at.strftime("%H:%M:%S") if j.created_at else ""

            clean_text = (j.input_text or "").replace("\n", " ").strip()
            preview = html.escape(clean_text[:35] + ("..." if len(clean_text) > 35 else ""))

            job_entry = f"• <b>{st_icon}</b> <code>#{j.id[:8]}</code> · {user_label}\n  🎙 <code>{model_name}</code> ({dur}) | 🕒 <code>{ts_str}</code>\n  💬 <i>\"{preview}\"</i>"
            if st == JobStatus.FAILED.value and j.error_message:
                brief_err = html.escape(str(j.error_message)[:60])
                job_entry += f"\n  ⚠️ <i>{brief_err}</i>"

            activity_lines.append(job_entry + "\n")

    full_text = base_text + "".join(activity_lines)
    kb = admin_queue_kb(
        lang=lang,
        failed_count=failed,
        running=running,
        queued=queued,
        success=success,
    )
    try:
        await callback.message.edit_text(full_text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(full_text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:queue_view:"))
async def handle_admin_queue_view(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    cat_key = callback.data.split(":")[2]  # "running", "queued", "success"
    status_map = {
        "running": JobStatus.RUNNING.value,
        "queued": JobStatus.QUEUED.value,
        "success": JobStatus.SUCCESS.value,
    }
    target_status = status_map.get(cat_key, JobStatus.RUNNING.value)

    async def _fetch_category_jobs(s: AsyncSession):
        user, _ = await get_or_create_user(s, callback.from_user.id)
        lang = user.language_code
        jobs = list((await s.execute(
            select(Job).where(Job.status == target_status).order_by(Job.created_at.desc()).limit(15)
        )).scalars().all())

        user_ids = {j.user_id for j in jobs}
        model_ids = {j.model_id for j in jobs if j.model_id}

        users_map = {}
        if user_ids:
            u_res = (await s.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
            users_map = {u.id: u for u in u_res}

        models_map = {}
        if model_ids:
            m_res = (await s.execute(select(VoiceModel).where(VoiceModel.id.in_(model_ids)))).scalars().all()
            models_map = {m.id: m for m in m_res}

        return lang, jobs, users_map, models_map

    if session is not None:
        lang, jobs, users_map, models_map = await _fetch_category_jobs(session)
    else:
        async with AsyncSessionLocal() as s:
            lang, jobs, users_map, models_map = await _fetch_category_jobs(s)

    title_key = f"admin_queue_list_title_{cat_key}"
    title_text = t(title_key, lang, count=len(jobs)) if t(title_key, lang) != title_key else f"📋 <b>{cat_key.upper()} ({len(jobs)})</b>\n\nSelect a job to view details:"

    if not jobs:
        text = f"{title_text}\n\n{t('admin_queue_list_empty', lang)}"
    else:
        lines = [title_text]
        for j in jobs:
            u = users_map.get(j.user_id)
            if u and u.username:
                user_label = f"@{html.escape(u.username)}"
            elif u and u.first_name:
                user_label = f"{html.escape(u.first_name)} (<code>{j.user_id}</code>)"
            else:
                user_label = f"<code>{j.user_id}</code>"

            m = models_map.get(j.model_id) if j.model_id else None
            model_name = html.escape(m.name if m else (j.edge_voice or "Custom"))
            dur = f"{j.seconds_consumed}s" if j.seconds_consumed > 0 else f"{j.seconds_estimated}s"
            ts_str = j.created_at.strftime("%Y-%m-%d %H:%M UTC") if j.created_at else "Unknown"
            clean_text = (j.input_text or "").replace("\n", " ").strip()
            preview = html.escape(clean_text[:40] + ("..." if len(clean_text) > 40 else ""))

            lines.append(
                f"• Job <code>#{j.id[:8]}</code> · {user_label}\n"
                f"  🎙 <code>{model_name}</code> ({dur}) | 🕒 <code>{ts_str}</code>\n"
                f"  💬 <i>\"{preview}\"</i>\n"
            )
        lines.append("<i>Click a job button below to inspect full details:</i>")
        text = "\n".join(lines)

    kb = admin_queue_list_kb(jobs=jobs, category=cat_key, lang=lang)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data == "admin:queue_failures")
async def handle_admin_queue_failures(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        failed_jobs = list((await session.execute(
            select(Job).where(Job.status == JobStatus.FAILED.value).order_by(Job.created_at.desc()).limit(10)
        )).scalars().all())
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            failed_jobs = list((await s.execute(
                select(Job).where(Job.status == JobStatus.FAILED.value).order_by(Job.created_at.desc()).limit(10)
            )).scalars().all())

    if not failed_jobs:
        text = t("admin_queue_failures_empty", lang)
    else:
        lines = [t("admin_queue_failures_title", lang, count=len(failed_jobs))]
        for j in failed_jobs:
            ts_str = j.created_at.strftime("%Y-%m-%d %H:%M UTC") if j.created_at else "Unknown"
            safe_err = html.escape(str(j.error_message or "Unknown error")[:120])
            lines.append(f"• Job <code>#{j.id[:8]}</code> (User <code>{j.user_id}</code>) at <code>{ts_str}</code>\n  <i>{safe_err}</i>\n")
        lines.append("<i>Click a job button below to inspect failure details or retry:</i>")
        text = "\n".join(lines)

    kb = admin_queue_failures_kb(jobs=failed_jobs, lang=lang)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data.in_({"admin:queue_recover_prompt", "admin:queue_recover_confirm"}))
async def handle_admin_queue_recover_disabled(callback: CallbackQuery):
    await callback.answer(
        "Interactive queue recovery is disabled for safety. Use admin retry controls on individual failed jobs instead.",
        show_alert=True,
    )


@admin_router.callback_query(F.data.startswith("admin:job_detail:"))
async def handle_admin_job_detail(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    job_id = callback.data.split(":")[2]

    async def _fetch_job_detail(s: AsyncSession):
        user, _ = await get_or_create_user(s, callback.from_user.id)
        lang = user.language_code
        job = await s.get(Job, job_id)
        target_user = await s.get(User, job.user_id) if job else None
        model = await s.get(VoiceModel, job.model_id) if (job and job.model_id) else None
        return lang, job, target_user, model

    if session is not None:
        lang, job, target_user, model = await _fetch_job_detail(session)
    else:
        async with AsyncSessionLocal() as s:
            lang, job, target_user, model = await _fetch_job_detail(s)

    if not job:
        await callback.answer("Job not found", show_alert=True)
        return

    can_retry, category, reason = evaluate_job_retry_eligibility(job, target_user)
    created_str = job.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if job.created_at else "Unknown"
    started_str = job.started_at.strftime("%Y-%m-%d %H:%M:%S UTC") if job.started_at else "Not started"
    finished_str = job.finished_at.strftime("%Y-%m-%d %H:%M:%S UTC") if job.finished_at else "Not finished"
    safe_err = html.escape(str(job.error_message or "None"))
    text_preview = html.escape(str(job.input_text or "")[:250])
    model_name = html.escape(model.name if model else (job.edge_voice or "Custom"))

    if target_user:
        u_handle = f"@{html.escape(target_user.username)}" if target_user.username else f"User {target_user.id}"
        u_name = html.escape(target_user.first_name or "")
        user_display = f"{u_name} ({u_handle})" if u_name else u_handle
        tier_badge = "💎 PURCHASED" if target_user.account_category == AccountCategory.PURCHASED.value else "🆓 FREE"
        bal_seconds = getattr(target_user, "balance_seconds", 0) or 0
        bal_text = f"{bal_seconds}s (~{round(bal_seconds / 60, 1)} မိနစ်)"
    else:
        user_display = f"User <code>{job.user_id}</code>"
        tier_badge = "UNKNOWN"
        bal_text = "0s"

    st_upper = str(job.status).upper()
    if st_upper == JobStatus.SUCCESS.value:
        st_badge = "✅ အောင်မြင် (SUCCESS)"
    elif st_upper == JobStatus.RUNNING.value:
        st_badge = "⚙️ လုပ်ဆောင်ဆဲ (RUNNING)"
    elif st_upper == JobStatus.QUEUED.value:
        st_badge = "⏳ စောင့်ဆိုင်းဆဲ (QUEUED)"
    elif st_upper == JobStatus.FAILED.value:
        st_badge = "❌ မအောင်မြင် (FAILED)"
    else:
        st_badge = f"🚫 {st_upper}"

    # Burmese Error diagnostics
    if job.status == JobStatus.FAILED.value:
        if category == "SYSTEM_FAILURE":
            diag_title = "ဆာဗာ / AI စနစ်ပိုင်းဆိုင်ရာ ယာယီချို့ယွင်းချက် (System Error)"
            diag_desc = "Applio, Edge-TTS သို့မဟုတ် Server restart/timeout ကြောင့် ဖြစ်ပွားခဲ့ခြင်းဖြစ်ပါသည်။ အသုံးပြုသူထံမှ မိနစ်နုတ်ယူခြင်း မရှိခဲ့ပါ။"
            retry_badge = "🟢 <b>Retry ပြန်လည်လုပ်ဆောင်နိုင်ပါသည် (Eligible for Retry)</b>\n<i>Admin အနေဖြင့် အောက်ပါခလုတ်ကို နှိပ်၍ Queue ထဲသို့ အသစ်ပြန်ထည့်ပေးနိုင်ပါသည်။</i>"
        elif category == "POLICY_USER_ERROR":
            diag_title = "မူဝါဒ သို့မဟုတ် အသုံးပြုသူဆိုင်ရာ ကန့်သတ်ချက် (Policy / User Error)"
            diag_desc = html.escape(reason)
            retry_badge = f"🔴 <b>Retry ပြုလုပ်၍ မရပါ (Ineligible):</b> <i>{html.escape(reason)}</i>"
        elif category == "USER_INACTIVE":
            diag_title = "အသုံးပြုသူ အကောင့် ပိတ်ပင်ခံထားရခြင်း (User Suspended)"
            diag_desc = "အသုံးပြုသူအား Admin မှ Block ပြုလုပ်ထားသဖြင့် ဆက်လက်ဆောင်ရွက်ခွင့် မပြုတော့ပါ။"
            retry_badge = "🔴 <b>Retry ပြုလုပ်၍ မရပါ (Ineligible)</b>"
        else:
            diag_title = "အခြား အခြေအနေ"
            diag_desc = html.escape(reason)
            retry_badge = f"🔴 <b>Ineligible:</b> {html.escape(reason)}"
    else:
        diag_title = "မရှိပါ (No Error)"
        diag_desc = "အလုပ် ချောမောစွာ ပြီးစီးခဲ့ပါသည် သို့မဟုတ် လုပ်ဆောင်ဆဲ ဖြစ်ပါသည်။"
        retry_badge = "⚪ <b>Ineligible (Status is not FAILED)</b>"

    detail_lines = [
        "📋 <b>Job Failure Diagnostics</b>" if job.status == JobStatus.FAILED.value else "📋 <b>Job Diagnostics & Details</b>",
        "",
        f"• Job ID: <code>{job.id}</code>",
        f"• User ID: <code>{job.user_id}</code>",
        f"• Status: <b>{job.status}</b> ({st_badge})",
        f"• Voice / Model: <code>{job.edge_voice}</code> (Model ID: <code>{job.model_id}</code> · <b>{model_name}</b>)",
        f"• User Details: {user_display} | {tier_badge} | လက်ကျန်: <code>{bal_text}</code>",
        f"• Speed / Pitch: <code>{job.speed_preset or 'normal'}</code> / <code>{job.gender_preset or 'm2m'}</code>",
        f"• Consumed Seconds: <b>{job.seconds_consumed}s</b> (Estimated: <code>{job.seconds_estimated}s</code>)",
        f"• Created: <code>{created_str}</code>",
        f"• Started: <code>{started_str}</code>",
        f"• Finished: <code>{finished_str}</code>",
        f"• Input Text Preview: <i>\"{text_preview}\"</i>",
        "",
        "🔍 <b>Error Analysis:</b>",
        f"• Category: <code>{category}</code> ({diag_title})",
        f"• Explanation: <i>{diag_desc}</i>",
        f"• Sanitized Error: <i>{safe_err}</i>",
        "",
        "⚙️ <b>Retry Eligibility:</b>",
        f"{retry_badge}",
    ]
    detail_text = "\n".join(detail_lines)

    kb = admin_job_detail_kb(job_id=job.id, is_retryable=can_retry, lang=lang, status=job.status)
    try:
        await callback.message.edit_text(detail_text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(detail_text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:job_retry_prompt:"))
async def handle_admin_job_retry_prompt(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    job_id = callback.data.split(":")[2]
    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        job = await session.get(Job, job_id)
        target_user = await session.get(User, job.user_id) if job else None
        model = await session.get(VoiceModel, job.model_id) if (job and job.model_id) else None
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            job = await s.get(Job, job_id)
            target_user = await s.get(User, job.user_id) if job else None
            model = await s.get(VoiceModel, job.model_id) if (job and job.model_id) else None

    if not job:
        await callback.answer("Job not found", show_alert=True)
        return

    can_retry, category, reason = evaluate_job_retry_eligibility(job, target_user)
    if not can_retry:
        await callback.answer(f"Cannot retry job: {reason}", show_alert=True)
        return

    model_name = html.escape(model.name if model else (job.edge_voice or "Custom"))
    user_label = f"@{html.escape(target_user.username)}" if (target_user and target_user.username) else f"<code>{job.user_id}</code>"

    prompt_text = (
        f"⚠️ <b>Confirm Job Re-Queue & Retry</b>\n\n"
        f"• Job ID: <code>{job.id}</code>\n"
        f"• Target User: <code>{job.user_id}</code> ({user_label})\n"
        f"• Model ID: <code>{job.model_id}</code> (<b>{model_name}</b>)\n"
        f"• Error Category: <code>{category}</code> (System Error)\n\n"
        f"<i>ဤအလုပ်ကို တန်းစီဇယား (Queue) ထဲသို့ အသစ်ပြန်လည် ထည့်သွင်းပေးမည်လား?</i>\n"
        f"• Job အခြေအနေသည် <b>FAILED</b> မှ <b>QUEUED</b> သို့ ပြောင်းလဲသွားပါမည်။\n"
        f"• Background Audio Worker မှ အလိုအလျောက် ရယူပြီး အသံပြောင်းလဲထုတ်လုပ်ပေးပါမည်။\n"
        f"• Balance is only deducted upon successful, validated audio generation."
    )
    kb = admin_job_retry_confirm_kb(job_id=job.id, lang=lang)
    await callback.message.edit_text(prompt_text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:job_retry_confirm:"))
async def handle_admin_job_retry_confirm(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    job_id = callback.data.split(":")[2]
    admin_id = callback.from_user.id

    async def _do_retry(s: AsyncSession) -> tuple[bool, str]:
        j = await s.get(Job, job_id)
        if not j:
            return False, "Job not found"
        target_u = await s.get(User, j.user_id)
        can_r, cat, rsn = evaluate_job_retry_eligibility(j, target_u)
        if not can_r:
            return False, rsn

        # Atomically reset job state for worker pick up
        j.status = JobStatus.QUEUED.value
        j.error_message = None
        j.started_at = None
        j.finished_at = None

        s.add(AuditLog(
            admin_id=admin_id,
            action="queue.job_retry",
            target_type="job",
            target_id=str(j.id),
            details_json=f'{{"user_id": {j.user_id}, "model_id": {j.model_id}, "category": "{cat}"}}',
        ))
        await s.commit()
        return True, "SUCCESS"

    if session is not None:
        ok, msg = await _do_retry(session)
    else:
        async with AsyncSessionLocal() as s:
            ok, msg = await _do_retry(s)

    if not ok:
        await callback.answer(f"Retry failed: {msg}", show_alert=True)
        return

    await callback.answer(f"✅ Job #{job_id[:8]} re-queued for processing!", show_alert=False)
    await handle_admin_queue(callback, session=session)


@admin_router.callback_query(F.data.startswith("admin:user_ledger:"))
async def handle_admin_user_ledger(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    parts = callback.data.split(":")
    try:
        target_user_id = int(parts[2])
        return_page = int(parts[3]) if len(parts) > 3 else 1
    except (IndexError, ValueError):
        await callback.answer("Malformed user callback", show_alert=True)
        return

    if session is not None:
        admin_user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = admin_user.language_code
        target_user = await session.get(User, target_user_id)
        events = await get_user_recent_ledger_events(session, target_user_id, limit=10)
    else:
        async with AsyncSessionLocal() as s:
            admin_user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = admin_user.language_code
            target_user = await s.get(User, target_user_id)
            events = await get_user_recent_ledger_events(s, target_user_id, limit=10)

    if not target_user:
        await callback.answer("User not found", show_alert=True)
        return

    text = f"📜 <b>Ledger History for User <code>{target_user.id}</code></b>\n\n"
    if events:
        for ev in events:
            ts_str = ev.created_at.strftime("%Y-%m-%d %H:%M UTC") if ev.created_at else "Unknown"
            safe_note = html.escape(ev.note or "No note")
            ev_min = round(ev.seconds / 60.0, 1)
            ref_str = f" [Ref: <code>{html.escape(str(ev.reference_id)[:16])}</code>]" if ev.reference_id else ""
            text += f"• <code>{ts_str}</code> · <b>{ev.direction}</b> {ev_min}m ({ev.seconds}s) [{ev.source}]{ref_str}\n  <i>{safe_note}</i>\n"
    else:
        text += "<i>No ledger events recorded for this user.</i>\n"

    kb = admin_user_ledger_kb(target_user_id=target_user.id, return_page=return_page, lang=lang)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# ---------------------------------------------------------------------------
# User Management & Detail Handlers
# ---------------------------------------------------------------------------

async def render_admin_users_page(
    callback: CallbackQuery,
    page: int = 1,
    session: AsyncSession | None = None,
    answer_callback: bool = True,
):
    if not await is_authorized_admin(callback):
        return

    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        users, total_users, total_pages = await get_paginated_users(session, page=page, page_size=5)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            users, total_users, total_pages = await get_paginated_users(s, page=page, page_size=5)

    clamped_page = max(1, min(page, total_pages))
    header = (
        f"👥 <b>User Management</b> (Page {clamped_page}/{total_pages})\n\n"
        f"Total Registered Users: <b>{total_users}</b>\n\n"
        f"Click a user below to inspect minute balances, account status, and ledger events:"
    )
    if not users:
        header += "\n\n<i>No registered users found.</i>"

    kb = admin_users_list_kb(users=users, page=clamped_page, total_pages=total_pages, lang=lang)
    try:
        await callback.message.edit_text(header, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(header, reply_markup=kb, parse_mode="HTML")
    if answer_callback:
        await callback.answer()


@admin_router.callback_query(F.data == "admin:users")
async def handle_admin_users(callback: CallbackQuery, session: AsyncSession | None = None):
    await render_admin_users_page(callback, page=1, session=session)


@admin_router.callback_query(F.data.startswith("admin:users_page:"))
async def handle_admin_users_page(callback: CallbackQuery, session: AsyncSession | None = None):
    try:
        page = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        page = 1
    await render_admin_users_page(callback, page=page, session=session)


async def render_admin_user_detail(
    event: CallbackQuery | Message,
    target_user_id: int,
    return_page: int = 1,
    session: AsyncSession | None = None,
    answer_callback: bool = True,
):
    if not await is_authorized_admin(event):
        return

    admin_id = event.from_user.id
    if session is not None:
        admin_user, _ = await get_or_create_user(session, admin_id)
        lang = admin_user.language_code
        target_user = await session.get(User, target_user_id)
        if not target_user:
            if isinstance(event, CallbackQuery):
                await event.answer("User not found", show_alert=True)
            else:
                await event.answer("User not found")
            return
        balance = await get_minute_balance(session, target_user.id)
        events = await get_user_recent_ledger_events(session, target_user.id, limit=3)
    else:
        async with AsyncSessionLocal() as s:
            admin_user, _ = await get_or_create_user(s, admin_id)
            lang = admin_user.language_code
            target_user = await s.get(User, target_user_id)
            if not target_user:
                if isinstance(event, CallbackQuery):
                    await event.answer("User not found", show_alert=True)
                else:
                    await event.answer("User not found")
                return
            balance = await get_minute_balance(s, target_user.id)
            events = await get_user_recent_ledger_events(s, target_user.id, limit=3)

    safe_username = f"@{html.escape(target_user.username)}" if target_user.username else "<i>None</i>"
    safe_first_name = html.escape(target_user.first_name) if target_user.first_name else "<i>None</i>"
    consumed_min = round(balance.consumed_seconds / 60.0, 1)

    text = (
        f"👤 <b>User Detail Card</b>\n\n"
        f"• Telegram ID: <code>{target_user.id}</code>\n"
        f"• Username: {safe_username}\n"
        f"• First Name: {safe_first_name}\n"
        f"• Account Status: <b>{target_user.account_category}</b>\n"
        f"• State: <code>{target_user.status}</code> · Lang: <code>{target_user.language_code}</code>\n\n"
        f"⏱️ <b>Minute Balances:</b>\n"
        f"• Total Usable: <b>{balance.total_usable_minutes:.1f}m</b> ({balance.total_usable_seconds}s)\n"
        f"• Monthly Free: <b>{balance.monthly_free_minutes:.1f}m</b> ({balance.monthly_free_seconds}s) [Period: {balance.last_monthly_grant_period or 'None'}]\n"
        f"• Purchased: <b>{balance.purchased_minutes:.1f}m</b> ({balance.purchased_seconds}s)\n"
        f"• Welcome: <b>{balance.welcome_minutes:.1f}m</b> ({balance.welcome_seconds}s)\n"
        f"• Referral: <b>{balance.referral_minutes:.1f}m</b> ({balance.referral_seconds}s)\n"
        f"• Consumed: <b>{consumed_min:.1f}m</b> ({balance.consumed_seconds}s)\n\n"
        f"📜 <b>Recent Ledger Events (Latest {len(events)}):</b>\n"
    )

    if events:
        for ev in events:
            ts_str = ev.created_at.strftime("%Y-%m-%d %H:%M UTC") if ev.created_at else "Unknown"
            safe_note = html.escape(ev.note or "No note")
            ev_min = round(ev.seconds / 60.0, 1)
            text += f"• <code>{ts_str}</code> · <b>{ev.direction}</b> {ev_min}m [{ev.source}]\n  <i>{safe_note}</i>\n"
    else:
        text += "• <i>No ledger events recorded yet.</i>\n"

    kb = admin_user_card_kb(
        target_user_id=target_user.id,
        return_page=return_page,
        lang=lang,
        user_status=target_user.status,
        account_category=target_user.account_category,
    )

    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await event.message.answer(text, reply_markup=kb, parse_mode="HTML")
        if answer_callback:
            await event.answer()
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


@admin_router.callback_query(F.data.startswith("admin:user_detail:"))
async def handle_admin_user_detail(
    callback: CallbackQuery,
    session: AsyncSession | None = None,
    answer_callback: bool = True,
):
    if not await is_authorized_admin(callback):
        return

    parts = callback.data.split(":")
    try:
        target_user_id = int(parts[2])
        return_page = int(parts[3]) if len(parts) > 3 else 1
    except (IndexError, ValueError):
        await callback.answer("Malformed user callback", show_alert=True)
        return

    await render_admin_user_detail(
        callback,
        target_user_id=target_user_id,
        return_page=return_page,
        session=session,
        answer_callback=answer_callback,
    )


@admin_router.callback_query(F.data.startswith("admin:user_status:"))
async def handle_admin_user_toggle_status(
    callback: CallbackQuery,
    session: AsyncSession | None = None,
):
    if not await is_authorized_admin(callback):
        return

    parts = callback.data.split(":")
    try:
        target_user_id = int(parts[2])
        action = parts[3].upper()
        return_page = int(parts[4]) if len(parts) > 4 else 1
    except (IndexError, ValueError):
        await callback.answer("Malformed status callback", show_alert=True)
        return

    if target_user_id == callback.from_user.id:
        await callback.answer(t("admin_alert_cannot_block_self", "en"), show_alert=True)
        return

    new_status = UserStatus.BLOCKED.value if action == "BLOCK" else UserStatus.ACTIVE.value

    if session is not None:
        admin_user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = admin_user.language_code
        target_user = await session.get(User, target_user_id)
        if not target_user:
            await callback.answer("User not found", show_alert=True)
            return
        target_user.status = new_status
        session.add(AuditLog(
            admin_id=callback.from_user.id,
            action="user.block" if action == "BLOCK" else "user.unblock",
            target_type="user",
            target_id=str(target_user.id),
            details_json=f'{{"new_status": "{new_status}"}}',
        ))
        await session.flush()
    else:
        async with AsyncSessionLocal() as s:
            admin_user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = admin_user.language_code
            target_user = await s.get(User, target_user_id)
            if not target_user:
                await callback.answer("User not found", show_alert=True)
                return
            target_user.status = new_status
            s.add(AuditLog(
                admin_id=callback.from_user.id,
                action="user.block" if action == "BLOCK" else "user.unblock",
                target_type="user",
                target_id=str(target_user.id),
                details_json=f'{{"new_status": "{new_status}"}}',
            ))
            await s.commit()

    await callback.answer(t("admin_alert_user_status_updated", lang, status=new_status), show_alert=False)
    await render_admin_user_detail(
        callback,
        target_user_id=target_user_id,
        return_page=return_page,
        session=session,
        answer_callback=False,
    )


@admin_router.callback_query(F.data.startswith("admin:user_tier:"))
async def handle_admin_user_toggle_tier(
    callback: CallbackQuery,
    session: AsyncSession | None = None,
):
    if not await is_authorized_admin(callback):
        return

    parts = callback.data.split(":")
    try:
        target_user_id = int(parts[2])
        return_page = int(parts[3]) if len(parts) > 3 else 1
    except (IndexError, ValueError):
        await callback.answer("Malformed tier callback", show_alert=True)
        return

    if session is not None:
        target_user = await session.get(User, target_user_id)
        if not target_user:
            await callback.answer("User not found", show_alert=True)
            return
        new_tier = AccountCategory.PURCHASED.value if target_user.account_category == AccountCategory.FREE.value else AccountCategory.FREE.value
        target_user.account_category = new_tier
        target_user.updated_at = utc_now()
        session.add(AuditLog(
            admin_id=callback.from_user.id,
            action="user.tier_toggle",
            target_type="user",
            target_id=str(target_user.id),
            details_json=f'{{"new_tier": "{new_tier}"}}',
        ))
        await session.flush()
    else:
        async with AsyncSessionLocal() as s:
            target_user = await s.get(User, target_user_id)
            if not target_user:
                await callback.answer("User not found", show_alert=True)
                return
            new_tier = AccountCategory.PURCHASED.value if target_user.account_category == AccountCategory.FREE.value else AccountCategory.FREE.value
            target_user.account_category = new_tier
            target_user.updated_at = utc_now()
            s.add(AuditLog(
                admin_id=callback.from_user.id,
                action="user.tier_toggle",
                target_type="user",
                target_id=str(target_user.id),
                details_json=f'{{"new_tier": "{new_tier}"}}',
            ))
            await s.commit()

    await callback.answer(f"⭐ User tier switched to {new_tier}!", show_alert=False)
    await render_admin_user_detail(
        callback,
        target_user_id=target_user_id,
        return_page=return_page,
        session=session,
        answer_callback=False,
    )


@admin_router.callback_query(F.data.startswith("admin:user_quick_credit:"))
async def handle_admin_user_quick_credit(
    callback: CallbackQuery,
    session: AsyncSession | None = None,
):
    if not await is_authorized_admin(callback):
        return

    parts = callback.data.split(":")
    try:
        target_user_id = int(parts[2])
        mins = int(parts[3])
        return_page = int(parts[4]) if len(parts) > 4 else 1
    except (IndexError, ValueError):
        await callback.answer("Malformed quick credit callback", show_alert=True)
        return

    sec = mins * 60
    if session is not None:
        target_user = await session.get(User, target_user_id)
        if not target_user:
            await callback.answer("User not found", show_alert=True)
            return
        src = LedgerSource.PURCHASE if target_user.account_category == AccountCategory.PURCHASED.value else LedgerSource.SYSTEM_GRANT
        await admin_adjust_balance(
            session=session,
            user_id=target_user_id,
            admin_id=callback.from_user.id,
            direction=LedgerDirection.CREDIT,
            seconds=sec,
            source=src,
            reason=f"Admin quick bonus +{mins}m",
        )
        await session.flush()
    else:
        async with AsyncSessionLocal() as s:
            target_user = await s.get(User, target_user_id)
            if not target_user:
                await callback.answer("User not found", show_alert=True)
                return
            src = LedgerSource.PURCHASE if target_user.account_category == AccountCategory.PURCHASED.value else LedgerSource.SYSTEM_GRANT
            await admin_adjust_balance(
                session=s,
                user_id=target_user_id,
                admin_id=callback.from_user.id,
                direction=LedgerDirection.CREDIT,
                seconds=sec,
                source=src,
                reason=f"Admin quick bonus +{mins}m",
            )
            await s.commit()

    await callback.answer(f"⚡ Added +{mins}m credit successfully!", show_alert=False)
    await render_admin_user_detail(
        callback,
        target_user_id=target_user_id,
        return_page=return_page,
        session=session,
        answer_callback=False,
    )



@admin_router.callback_query(F.data == "admin:user_search")
async def handle_admin_user_search_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    if session is not None:
        admin_user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = admin_user.language_code
    else:
        async with AsyncSessionLocal() as s:
            admin_user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = admin_user.language_code

    await state.set_state(AdminUserSearchFSM.waiting_for_query)
    text = t("admin_user_search_prompt", lang)
    kb = admin_user_search_cancel_kb(lang=lang)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data == "admin:user_search_cancel")
async def handle_admin_user_search_cancel(callback: CallbackQuery, state: FSMContext, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    await state.clear()
    await callback.answer(t("admin_alert_search_cancelled", "en"))
    await render_admin_users_page(callback, page=1, session=session, answer_callback=False)


@admin_router.message(AdminUserSearchFSM.waiting_for_query)
async def handle_admin_user_search_query(message: Message, state: FSMContext, session: AsyncSession | None = None):
    if not await is_authorized_admin(message):
        return

    query = (message.text or "").strip()
    if not query:
        await message.answer("Please enter a valid Telegram ID or @username.")
        return

    await state.clear()
    await _execute_user_lookup(message, query=query, session=session)


@admin_router.message(Command("user"))
async def handle_admin_user_command(message: Message, session: AsyncSession | None = None):
    if not await is_authorized_admin(message):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: <code>/user &lt;telegram_id or @username&gt;</code>", parse_mode="HTML")
        return

    query = parts[1].strip()
    await _execute_user_lookup(message, query=query, session=session)


async def _execute_user_lookup(message: Message, query: str, session: AsyncSession | None = None):
    clean_query = query.lstrip("@").strip()
    target_user = None

    if session is not None:
        admin_user, _ = await get_or_create_user(session, message.from_user.id)
        lang = admin_user.language_code
        if clean_query.isdigit():
            target_user = await session.get(User, int(clean_query))
        if not target_user:
            target_user = (await session.execute(
                select(User).where(func.lower(User.username) == clean_query.lower())
            )).scalar_one_or_none()
    else:
        async with AsyncSessionLocal() as s:
            admin_user, _ = await get_or_create_user(s, message.from_user.id)
            lang = admin_user.language_code
            if clean_query.isdigit():
                target_user = await s.get(User, int(clean_query))
            if not target_user:
                target_user = (await s.execute(
                    select(User).where(func.lower(User.username) == clean_query.lower())
                )).scalar_one_or_none()

    if not target_user:
        not_found_text = t("admin_user_search_not_found", lang, query=html.escape(query))
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t("admin_btn_search_user", lang), callback_data="admin:user_search")],
            [InlineKeyboardButton(text=t("btn_back", lang), callback_data="admin:users")],
        ])
        await message.answer(not_found_text, reply_markup=kb, parse_mode="HTML")
        return

    await render_admin_user_detail(message, target_user_id=target_user.id, return_page=1, session=session)


# ---------------------------------------------------------------------------
# Manual Minute Adjustment Handlers (FSM)
# ---------------------------------------------------------------------------

@admin_router.callback_query(F.data.startswith("admin:adjust_start:"))
async def handle_admin_adjust_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession | None = None,
):
    if not await is_authorized_admin(callback):
        return

    parts = callback.data.split(":")
    try:
        target_user_id = int(parts[2])
        direction = parts[3].upper()
        return_page = int(parts[4]) if len(parts) > 4 else 1
    except (IndexError, ValueError):
        await callback.answer("Malformed adjustment start callback", show_alert=True)
        return

    if direction not in ("CREDIT", "DEBIT"):
        await callback.answer("Invalid adjustment direction", show_alert=True)
        return

    if session is not None:
        admin_user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = admin_user.language_code
        target_user = await session.get(User, target_user_id)
        if not target_user:
            await callback.answer("Target user not found", show_alert=True)
            return
        balance = await get_minute_balance(session, target_user.id)
    else:
        async with AsyncSessionLocal() as s:
            admin_user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = admin_user.language_code
            target_user = await s.get(User, target_user_id)
            if not target_user:
                await callback.answer("Target user not found", show_alert=True)
                return
            balance = await get_minute_balance(s, target_user.id)

    await state.set_state(AdminAdjustFSM.waiting_for_amount)
    await state.update_data(
        target_user_id=target_user.id,
        direction=direction,
        return_page=return_page,
        lang=lang,
        admin_id=callback.from_user.id,
        target_username=target_user.username or "",
        purchased_seconds=balance.purchased_seconds,
        total_usable_seconds=balance.total_usable_seconds,
        account_category=target_user.account_category,
    )

    action_label = "Credit (+)" if direction == "CREDIT" else "Debit (-)"
    text = (
        f"⚙️ <b>Manual Minute Adjustment — Step 1/2</b>\n\n"
        f"• Target User: <code>{target_user.id}</code> (@{html.escape(target_user.username or 'none')})\n"
        f"• Action: <b>{action_label}</b>\n"
        f"• Current Purchased: <b>{balance.purchased_minutes:.1f}m</b> ({balance.purchased_seconds}s)\n\n"
        f"Please reply with the number of <b>whole minutes</b> to {direction.lower()}:\n"
        f"(e.g., <code>5</code>, <code>10</code>, <code>30</code>)"
    )
    kb = admin_adjust_cancel_kb(lang=lang)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.message(AdminAdjustFSM.waiting_for_amount)
async def handle_admin_adjust_amount_input(
    message: Message,
    state: FSMContext,
    session: AsyncSession | None = None,
):
    if not await is_authorized_admin(message):
        return

    data = await state.get_data()
    lang = data.get("lang", "en")
    direction = data.get("direction", "CREDIT")
    target_user_id = data.get("target_user_id")
    raw_text = (message.text or "").strip()

    if not raw_text.isdigit() or int(raw_text) <= 0:
        await message.answer(
            "⚠️ Please enter a valid positive whole number of minutes (e.g. <code>5</code>, <code>10</code>, <code>60</code>):",
            reply_markup=admin_adjust_cancel_kb(lang=lang),
            parse_mode="HTML",
        )
        return

    minutes = int(raw_text)
    seconds_to_adjust = minutes * 60

    # For DEBIT, check that user has sufficient purchased balance
    if direction == "DEBIT":
        if session is not None:
            balance = await get_minute_balance(session, target_user_id)
        else:
            async with AsyncSessionLocal() as s:
                balance = await get_minute_balance(s, target_user_id)

        if balance.purchased_seconds < seconds_to_adjust:
            avail_min = balance.purchased_minutes
            await message.answer(
                f"⚠️ Cannot debit <b>{minutes}m</b>: user only has <b>{avail_min:.1f}m</b> purchased minutes.\n\n"
                f"Debits cannot create a negative balance. Please enter a smaller amount or cancel:",
                reply_markup=admin_adjust_cancel_kb(lang=lang),
                parse_mode="HTML",
            )
            return

    await state.update_data(minutes=minutes, seconds=seconds_to_adjust)
    await state.set_state(AdminAdjustFSM.waiting_for_reason)

    action_label = "Credit (+)" if direction == "CREDIT" else "Debit (-)"
    prompt = (
        f"⚙️ <b>Manual Minute Adjustment — Step 2/2</b>\n\n"
        f"• Target User: <code>{target_user_id}</code>\n"
        f"• Action: <b>{action_label} {minutes} minutes</b> ({seconds_to_adjust}s)\n\n"
        f"Please reply with the <b>mandatory reason</b> for this adjustment:\n"
        f"(e.g., <i>Support compensation for network timeout</i>, <i>Manual KBZPay top-up</i>)"
    )
    await message.answer(prompt, reply_markup=admin_adjust_cancel_kb(lang=lang), parse_mode="HTML")


@admin_router.message(AdminAdjustFSM.waiting_for_reason)
async def handle_admin_adjust_reason_input(
    message: Message,
    state: FSMContext,
    session: AsyncSession | None = None,
):
    if not await is_authorized_admin(message):
        return

    data = await state.get_data()
    lang = data.get("lang", "en")
    target_user_id = data.get("target_user_id")
    direction = data.get("direction", "CREDIT")
    minutes = data.get("minutes", 0)
    seconds = data.get("seconds", minutes * 60)
    target_username = data.get("target_username", "")

    clean_reason = (message.text or "").strip()
    if not clean_reason or len(clean_reason) < 3:
        await message.answer(
            "⚠️ Reason is mandatory (minimum 3 characters). Please enter the adjustment reason:",
            reply_markup=admin_adjust_cancel_kb(lang=lang),
            parse_mode="HTML",
        )
        return

    # Fetch live balance and user status for projection
    if session is not None:
        target_user = await session.get(User, target_user_id)
        balance = await get_minute_balance(session, target_user_id)
    else:
        async with AsyncSessionLocal() as s:
            target_user = await s.get(User, target_user_id)
            balance = await get_minute_balance(s, target_user_id)

    curr_purchased = balance.purchased_minutes
    curr_total = balance.total_usable_minutes
    curr_cat = target_user.account_category if target_user else "FREE"

    if direction == "CREDIT":
        proj_purchased = curr_purchased + minutes
        proj_total = curr_total + minutes
        proj_cat = "PURCHASED"
    else:
        proj_purchased = max(0.0, curr_purchased - minutes)
        proj_total = max(0.0, curr_total - minutes)
        proj_cat = "PURCHASED" if proj_purchased > 0 else "FREE"

    token = f"ADJ-{uuid.uuid4().hex[:10]}"
    await state.update_data(reason=clean_reason, token=token)
    await state.set_state(AdminAdjustFSM.confirming)

    safe_uname = f"@{html.escape(target_username)}" if target_username else "<i>none</i>"
    preview_text = (
        f"⚠️ <b>Confirm Minute Adjustment</b>\n\n"
        f"• Target User: <code>{target_user_id}</code> ({safe_uname})\n"
        f"• Action: <b>{direction}</b>\n"
        f"• Amount: <b>{minutes} minutes</b> ({seconds}s)\n"
        f"• Reason: <i>{html.escape(clean_reason)}</i>\n\n"
        f"📊 <b>Balance Projection:</b>\n"
        f"• Purchased: {curr_purchased:.1f}m → <b>{proj_purchased:.1f}m</b>\n"
        f"• Total Usable: {curr_total:.1f}m → <b>{proj_total:.1f}m</b>\n"
        f"• Account Status: {curr_cat} → <b>{proj_cat}</b>\n\n"
        f"<i>Are you sure you want to apply this adjustment?</i>"
    )
    kb = admin_adjust_preview_kb(token=token, lang=lang)
    await message.answer(preview_text, reply_markup=kb, parse_mode="HTML")


@admin_router.callback_query(F.data.startswith("admin:adjust_confirm:"))
async def handle_admin_adjust_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession | None = None,
):
    if not await is_authorized_admin(callback):
        return

    token = callback.data.split(":")[2]
    data = await state.get_data()
    expected_token = data.get("token")
    target_user_id = data.get("target_user_id")
    direction = data.get("direction")
    minutes = data.get("minutes")
    seconds = data.get("seconds")
    clean_reason = data.get("reason")
    return_page = data.get("return_page", 1)
    lang = data.get("lang", "en")

    if not expected_token or expected_token != token or not target_user_id or not minutes or not clean_reason:
        await callback.answer("⚠️ Adjustment confirmation is invalid or has expired.", show_alert=True)
        return

    # Clear state immediately to prevent duplicate button taps
    await state.clear()

    dir_enum = LedgerDirection(direction)
    admin_id = callback.from_user.id

    try:
        if session is not None:
            updated_bal = await admin_adjust_balance(
                session=session,
                user_id=target_user_id,
                admin_id=admin_id,
                seconds=seconds,
                source=LedgerSource.PURCHASE,
                direction=dir_enum,
                reason=clean_reason,
                idempotency_key=token,
            )
            await session.commit()
        else:
            async with AsyncSessionLocal() as s:
                updated_bal = await admin_adjust_balance(
                    session=s,
                    user_id=target_user_id,
                    admin_id=admin_id,
                    seconds=seconds,
                    source=LedgerSource.PURCHASE,
                    direction=dir_enum,
                    reason=clean_reason,
                    idempotency_key=token,
                )
                await s.commit()
    except Exception as e:
        if session is not None:
            try:
                await session.rollback()
            except Exception as rb_err:
                logger.warning("Rollback error after adjustment failure: %s", rb_err)
        logger.error("Failed to execute admin_adjust_balance: %s", e)
        await callback.answer(f"Adjustment failed: {e}", show_alert=True)
        return

    success_text = (
        f"✅ <b>Minute Adjustment Applied!</b>\n\n"
        f"• Target User: <code>{target_user_id}</code>\n"
        f"• Adjustment: <b>{direction} {minutes}m</b> ({seconds}s)\n"
        f"• New Purchased Balance: <b>{updated_bal.purchased_minutes:.1f}m</b> ({updated_bal.purchased_seconds}s)\n"
        f"• Total Usable: <b>{updated_bal.total_usable_minutes:.1f}m</b>\n"
        f"• Reason: <i>{html.escape(clean_reason)}</i>\n"
        f"• Idempotency Token: <code>{token}</code>"
    )
    back_kb = admin_user_card_kb(target_user_id=target_user_id, return_page=return_page, lang=lang)
    try:
        await callback.message.edit_text(success_text, reply_markup=back_kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(success_text, reply_markup=back_kb, parse_mode="HTML")
    await callback.answer(f"Adjusted {minutes} minutes successfully!", show_alert=False)


@admin_router.callback_query(F.data == "admin:adjust_cancel")
async def handle_admin_adjust_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession | None = None,
):
    if not await is_authorized_admin(callback):
        return

    data = await state.get_data()
    target_user_id = data.get("target_user_id")
    return_page = data.get("return_page", 1)
    await state.clear()

    await callback.answer("❌ Adjustment cancelled.", show_alert=True)
    if target_user_id:
        await render_admin_user_detail(
            callback,
            target_user_id=target_user_id,
            return_page=return_page,
            session=session,
            answer_callback=False,
        )
    else:
        await render_admin_users_page(callback, page=return_page, session=session, answer_callback=False)



@admin_router.callback_query(F.data == "admin:pricing")
async def handle_admin_pricing(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    from app.services.payments import get_active_pricing_plans, get_payment_account_info
    if session is not None:
        admin_user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = admin_user.language_code if admin_user else "my"
        plans = await get_active_pricing_plans(session)
        acc_info = await get_payment_account_info(session)
    else:
        async with AsyncSessionLocal() as s:
            admin_user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = admin_user.language_code if admin_user else "my"
            plans = await get_active_pricing_plans(s)
            acc_info = await get_payment_account_info(s)

    lines = [
        t("admin_pricing_title", lang),
        "📦 <b>Active Pricing Plans:</b>",
    ]
    buttons = []
    for p in plans:
        lines.append(f"• <b>{p.code}</b>: {p.minutes} mins — <b>{p.price_mmk:,} MMK</b>")
        buttons.append([
            InlineKeyboardButton(
                text=f"✏️ Edit {p.code} Price ({p.price_mmk:,} Ks)",
                callback_data=f"admin:price_edit:{p.code}",
            )
        ])

    lines.append(
        "\n🏦 <b>Configured Payment Accounts:</b>\n"
        f"• <b>KBZPay:</b> <code>{acc_info['kpay_number']}</code> ({acc_info['kpay_name']})\n"
        f"• <b>WavePay:</b> <code>{acc_info['wave_number']}</code> ({acc_info['wave_name']})"
    )

    buttons.append([InlineKeyboardButton(text="💳 Edit Payment Accounts", callback_data="admin:pay_acc_edit")])
    buttons.append([InlineKeyboardButton(text=t("admin_btn_back_to_payments", lang), callback_data="admin:payments")])
    buttons.append([InlineKeyboardButton(text=t("btn_admin_dashboard", lang), callback_data="admin:dashboard")])

    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:price_edit:"))
async def handle_admin_price_edit(callback: CallbackQuery):
    if not await is_authorized_admin(callback):
        return
    code = callback.data.split(":")[2]
    text = (
        f"✏️ <b>Edit Price for Package {code}</b>\n\n"
        "Select a quick price preset below, or tap Custom Price:"
    )
    await callback.message.edit_text(text, reply_markup=admin_pricing_plan_edit_kb(code), parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:price_set:"))
async def handle_admin_price_set(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    parts = callback.data.split(":")
    code = parts[2]
    new_price = int(parts[3])

    from app.services.payments import update_pricing_plan_price
    if session is not None:
        await update_pricing_plan_price(session, code, new_price, admin_id=callback.from_user.id)
        await session.commit()
    else:
        async with AsyncSessionLocal() as s:
            await update_pricing_plan_price(s, code, new_price, admin_id=callback.from_user.id)
            await s.commit()

    await callback.answer(f"✅ Price updated: {code} = {new_price:,} MMK", show_alert=False)
    await handle_admin_pricing(callback, session=session)


@admin_router.callback_query(F.data.startswith("admin:price_custom:"))
async def handle_admin_price_custom(callback: CallbackQuery, state: FSMContext):
    if not await is_authorized_admin(callback):
        return
    code = callback.data.split(":")[2]
    await state.set_state(AdminPriceEditFSM.waiting_for_price)
    await state.update_data(package_code=code)
    text = (
        f"✍️ <b>Custom Price for {code}</b>\n\n"
        "Enter new price in MMK (numbers only, e.g. 2500):"
    )
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Cancel", callback_data="admin:pricing")]
    ])
    await callback.message.edit_text(text, reply_markup=cancel_kb, parse_mode="HTML")
    await callback.answer()


@admin_router.message(AdminPriceEditFSM.waiting_for_price)
async def handle_admin_price_custom_receive(message: Message, state: FSMContext, session: AsyncSession | None = None):
    if not settings.is_admin(message.from_user.id):
        return
    data = await state.get_data()
    code = data.get("package_code")
    raw_val = message.text.strip().replace(",", "")
    if not raw_val.isdigit() or int(raw_val) < 100 or int(raw_val) > 10000000:
        await message.answer("❌ Invalid price. Please enter a valid number between 100 and 10,000,000 MMK:")
        return

    val = int(raw_val)
    from app.services.payments import update_pricing_plan_price
    if session is not None:
        admin_user, _ = await get_or_create_user(session, message.from_user.id)
        lang = admin_user.language_code if admin_user else "my"
        await update_pricing_plan_price(session, code, val, admin_id=message.from_user.id)
        await session.commit()
    else:
        async with AsyncSessionLocal() as s:
            admin_user, _ = await get_or_create_user(s, message.from_user.id)
            lang = admin_user.language_code if admin_user else "my"
            await update_pricing_plan_price(s, code, val, admin_id=message.from_user.id)
            await s.commit()

    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Back to Pricing", callback_data="admin:pricing")],
        [InlineKeyboardButton(text=t("btn_admin_dashboard", lang), callback_data="admin:dashboard")],
    ])
    await message.answer(f"✅ Price for <b>{code}</b> updated to <b>{val:,} MMK</b>!", reply_markup=kb, parse_mode="HTML")


@admin_router.callback_query(F.data == "admin:pay_acc_edit")
async def handle_admin_pay_accounts_menu(callback: CallbackQuery):
    if not await is_authorized_admin(callback):
        return
    text = (
        "💳 <b>Edit Payment Accounts (KBZPay / WavePay)</b>\n\n"
        "Select which account field you want to modify:"
    )
    await callback.message.edit_text(text, reply_markup=admin_payment_accounts_edit_kb(), parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:pay_field:"))
async def handle_admin_pay_field_edit(callback: CallbackQuery, state: FSMContext):
    if not await is_authorized_admin(callback):
        return
    field_key = callback.data.split(":")[2]
    field_labels = {
        "payment_kpay_number": "KBZPay Account Phone Number",
        "payment_kpay_name": "KBZPay Account Holder Name",
        "payment_wave_number": "WavePay Account Phone Number",
        "payment_wave_name": "WavePay Account Holder Name",
    }
    label = field_labels.get(field_key, field_key)

    await state.set_state(AdminSettingsFSM.waiting_for_value)
    await state.update_data(setting_key=field_key, return_to="admin:pricing")

    text = f"✍️ <b>Enter New Value for {label}</b>\n\nPlease send the new value as a text message below:"
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Cancel", callback_data="admin:pricing")]
    ])
    await callback.message.edit_text(text, reply_markup=cancel_kb, parse_mode="HTML")
    await callback.answer()



@admin_router.callback_query(F.data.in_({"admin:settings", "admin:policies"}))
async def handle_admin_settings(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    maint_status = "🔴 ACTIVE (Maintenance - Users Paused)" if settings.maintenance_mode else "🟢 LIVE (Accepting User Requests)"

    from app.services.payments import get_active_pricing_plans
    if session is not None:
        plans = await get_active_pricing_plans(session)
        admin_user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = admin_user.language_code
    else:
        async with AsyncSessionLocal() as s:
            plans = await get_active_pricing_plans(s)
            admin_user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = admin_user.language_code

    pkg_lines = []
    for p in plans:
        pkg_lines.append(f"• {p.minutes} Minutes: {p.price_mmk:,} MMK")
    minute_packages_display = "\n".join(pkg_lines) if pkg_lines else "• No active packages configured"

    text = (
        "⚙️ <b>Bot Runtime Settings & System Policies</b>\n\n"
        "🖥️ <b>Infrastructure & Runtime:</b>\n"
        f"• App: <code>{settings.app_name}</code> (App Env: <code>{settings.app_env}</code>)\n"
        f"• Operational Status: <b>{maint_status}</b>\n"
        f"• Log Level: <code>{settings.log_level}</code>\n"
        f"• Default TTS Voice: <code>{settings.default_tts_voice}</code>\n"
        f"• Channel Gate ID: <code>{settings.required_channel_id or 'Not Set'}</code>\n"
        f"• Group Gate ID: <code>{settings.required_group_id or 'Not Set'}</code>\n"
        f"• Applio Workdir: <code>{settings.applio_workdir}</code>\n"
        f"• Applio Python: <code>{settings.applio_python or 'Auto (<workdir>/env/python.exe)'}</code>\n\n"
        "🆓 <b>Free Tier Policy:</b>\n"
        f"• Monthly Allowance: <b>{settings.free_monthly_allowance_seconds // 60}m</b> ({settings.free_monthly_allowance_seconds}s per UTC calendar month)\n"
        f"• Free Char Limit: <b>{settings.character_limit_free} characters</b>\n"
        f"• Daily Job Limit: <b>{settings.free_daily_job_limit} jobs/day</b>\n"
        f"• Max Concurrent Jobs: <b>{settings.max_concurrent_jobs_per_user}</b>\n"
        "• Model Access: Admin-permitted Free models only\n\n"
        "💎 <b>Purchased Tier Policy:</b>\n"
        f"• Character Limit: <b>{settings.character_limit_purchased} characters</b>\n"
        "• Daily Job Limit: <b>Unlimited</b> (routine fair-use)\n"
        f"• Max Concurrent Jobs: <b>{settings.max_concurrent_jobs_per_user}</b>\n"
        "• Minute Expiry: <b>Non-expiring</b>\n"
        "• Model Access: Admin-permitted Purchased models\n\n"
        "💳 <b>Minute Packages:</b>\n"
        f"{minute_packages_display}\n\n"
        "⚖️ <b>Deduction & Billing Rules:</b>\n"
        "• Order: <code>monthly_free_seconds</code> ➔ <code>purchased_seconds</code>\n"
        "• Idempotent post-generation deduction per job ID\n"
        "• Failed or cancelled jobs are never deducted (auto-refunded)\n"
        "• Admin Bypass: Unlimited generations without balance deduction"
    )

    kb = admin_settings_interactive_kb(lang=lang, maintenance_mode=settings.maintenance_mode)

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data == "admin:settings:allowance")
async def handle_admin_settings_allowance(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    if session is not None:
        admin_user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = admin_user.language_code
    else:
        async with AsyncSessionLocal() as s:
            admin_user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = admin_user.language_code
    presets = [
        ("1 Min (60s)", "60"),
        ("2 Mins (120s)", "120"),
        ("3 Mins (180s)", "180"),
        ("5 Mins (300s)", "300"),
        ("10 Mins (600s)", "600"),
    ]
    kb = admin_setting_presets_kb("free_monthly_allowance_seconds", presets, allow_custom=True, lang=lang)
    text = (
        "⏱️ <b>Edit Free Tier Monthly Allowance</b>\n\n"
        f"Current Allowance: <b>{settings.free_monthly_allowance_seconds // 60}m ({settings.free_monthly_allowance_seconds}s)</b>\n\n"
        "Select a preset below or enter a custom duration in seconds:"
    )
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data == "admin:settings:free_chars")
async def handle_admin_settings_free_chars(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    if session is not None:
        admin_user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = admin_user.language_code
    else:
        async with AsyncSessionLocal() as s:
            admin_user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = admin_user.language_code
    presets = [
        ("250 Chars", "250"),
        ("500 Chars", "500"),
        ("750 Chars", "750"),
        ("1,000 Chars", "1000"),
    ]
    kb = admin_setting_presets_kb("character_limit_free", presets, allow_custom=True, lang=lang)
    text = (
        "📝 <b>Edit Free Tier Character Limit</b>\n\n"
        f"Current Limit: <b>{settings.character_limit_free} characters</b>\n\n"
        "Select a preset below or enter a custom character limit:"
    )
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data == "admin:settings:purchased_chars")
async def handle_admin_settings_purchased_chars(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    if session is not None:
        admin_user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = admin_user.language_code
    else:
        async with AsyncSessionLocal() as s:
            admin_user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = admin_user.language_code
    presets = [
        ("1,000 Chars", "1000"),
        ("2,000 Chars", "2000"),
        ("3,000 Chars", "3000"),
        ("5,000 Chars", "5000"),
    ]
    kb = admin_setting_presets_kb("character_limit_purchased", presets, allow_custom=True, lang=lang)
    text = (
        "💎 <b>Edit Purchased Tier Character Limit</b>\n\n"
        f"Current Limit: <b>{settings.character_limit_purchased} characters</b>\n\n"
        "Select a preset below or enter a custom character limit:"
    )
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data == "admin:settings:daily_limit")
async def handle_admin_settings_daily_limit(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    if session is not None:
        admin_user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = admin_user.language_code
    else:
        async with AsyncSessionLocal() as s:
            admin_user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = admin_user.language_code
    presets = [
        ("3 / day", "3"),
        ("5 / day", "5"),
        ("10 / day", "10"),
        ("20 / day", "20"),
        ("♾️ Unlimited", "0"),
    ]
    kb = admin_setting_presets_kb("free_daily_job_limit", presets, allow_custom=True, lang=lang)
    text = (
        "📊 <b>Edit Free Tier Daily Job Limit</b>\n\n"
        f"Current Limit: <b>{settings.free_daily_job_limit or 'Unlimited'} jobs/day</b>\n\n"
        "Select a preset below or enter a custom limit:"
    )
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data == "admin:settings:gates")
async def handle_admin_settings_gates(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    if session is not None:
        admin_user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = admin_user.language_code
    else:
        async with AsyncSessionLocal() as s:
            admin_user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = admin_user.language_code
    text = (
        "📢 <b>Channel & Group Membership Gates</b>\n\n"
        f"• <b>Required Channel ID:</b> <code>{settings.required_channel_id or 'Not Set'}</code>\n"
        f"• <b>Required Group ID:</b> <code>{settings.required_group_id or 'Not Set'}</code>\n"
        f"• <b>Channel Invite Link:</b> <code>{settings.channel_invite_link or 'Not Set'}</code>\n"
        f"• <b>Group Invite Link:</b> <code>{settings.group_invite_link or 'Not Set'}</code>\n\n"
        "Select an item to update:"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Edit Channel ID", callback_data="admin:settings_input:required_channel_id"),
                InlineKeyboardButton(text="✏️ Edit Group ID", callback_data="admin:settings_input:required_group_id"),
            ],
            [
                InlineKeyboardButton(text="✏️ Edit Channel Link", callback_data="admin:settings_input:channel_invite_link"),
                InlineKeyboardButton(text="✏️ Edit Group Link", callback_data="admin:settings_input:group_invite_link"),
            ],
            [
                InlineKeyboardButton(text=t("btn_back", lang), callback_data="admin:settings"),
            ],
        ]
    )
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data == "admin:settings:toggle_maintenance")
async def handle_admin_settings_toggle_maint(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    new_val = not settings.maintenance_mode
    if session is not None:
        await set_dynamic_setting(session, "maintenance_mode", new_val, admin_id=callback.from_user.id)
        await session.commit()
    else:
        async with AsyncSessionLocal() as s:
            await set_dynamic_setting(s, "maintenance_mode", new_val, admin_id=callback.from_user.id)
            await s.commit()

    alert_msg = f"🛠️ Maintenance Mode: {'ENABLED (Users Blocked) 🔴' if new_val else 'DISABLED (Live) 🟢'}"
    await callback.answer(alert_msg, show_alert=False)
    await handle_admin_settings(callback, session=session)


@admin_router.callback_query(F.data.startswith("admin:set:"))
async def handle_admin_settings_preset(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    parts = callback.data.split(":")
    if len(parts) < 4:
        await callback.answer("Invalid request.", show_alert=True)
        return
    key = parts[2]
    val = parts[3]

    if session is not None:
        await set_dynamic_setting(session, key, val, admin_id=callback.from_user.id)
        await session.commit()
    else:
        async with AsyncSessionLocal() as s:
            await set_dynamic_setting(s, key, val, admin_id=callback.from_user.id)
            await s.commit()

    await callback.answer(f"✅ Updated {key} = {val}", show_alert=False)
    await handle_admin_settings(callback, session=session)


@admin_router.callback_query(F.data.startswith("admin:settings_input:"))
async def handle_admin_settings_input_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    if session is not None:
        admin_user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = admin_user.language_code
    else:
        async with AsyncSessionLocal() as s:
            admin_user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = admin_user.language_code
    key = callback.data.split(":")[2]
    await state.set_state(AdminSettingsFSM.waiting_for_value)
    await state.update_data(setting_key=key)

    text = f"✍️ <b>Enter New Value for <code>{key}</code>:</b>\n\nPlease send the new value as a text message below:"
    await callback.message.edit_text(text, reply_markup=admin_settings_cancel_kb(lang=lang), parse_mode="HTML")
    await callback.answer()


@admin_router.message(AdminSettingsFSM.waiting_for_value)
async def handle_admin_settings_input_receive(message: Message, state: FSMContext, session: AsyncSession | None = None):
    if not settings.is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("setting_key")
    return_to = data.get("return_to")
    if not key:
        await state.clear()
        return

    raw_val = message.text.strip()
    from app.services.dynamic_settings import SETTING_TYPES
    expected_type = SETTING_TYPES.get(key, str)

    if expected_type is int:
        if not raw_val.isdigit():
            await message.answer(f"❌ Invalid value. <code>{key}</code> requires a positive integer. Please try again:")
            return
        parsed_val = int(raw_val)
    else:
        parsed_val = raw_val

    if session is not None:
        admin_user, _ = await get_or_create_user(session, message.from_user.id)
        lang = admin_user.language_code if admin_user else "my"
        await set_dynamic_setting(session, key, parsed_val, admin_id=message.from_user.id)
        await session.commit()
    else:
        async with AsyncSessionLocal() as s:
            admin_user, _ = await get_or_create_user(s, message.from_user.id)
            lang = admin_user.language_code if admin_user else "my"
            await set_dynamic_setting(s, key, parsed_val, admin_id=message.from_user.id)
            await s.commit()

    await state.clear()

    if return_to == "admin:pricing":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Back to Pricing", callback_data="admin:pricing")],
            [InlineKeyboardButton(text=t("btn_admin_dashboard", lang), callback_data="admin:dashboard")],
        ])
        await message.answer(
            f"✅ Payment account setting <code>{key}</code> successfully updated to: <b>{parsed_val}</b>",
            reply_markup=kb,
            parse_mode="HTML",
        )
        return

    kb = admin_settings_interactive_kb(lang=lang, maintenance_mode=settings.maintenance_mode)
    text = (
        "⚙️ <b>Bot Runtime Settings & System Policies</b>\n\n"
        f"• Setting <code>{key}</code> successfully updated to <b>{parsed_val}</b>.\n"
        "Tap below to continue configuring or return to dashboard:"
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")



# ---------------------------------------------------------------------------
# Guide Video Management Handlers
# ---------------------------------------------------------------------------

async def _get_tutorial_state(session: AsyncSession) -> tuple[str, bool]:
    link_row = (await session.execute(select(BotSetting).where(BotSetting.key == "tutorial_video_link"))).scalar_one_or_none()
    current_link = link_row.value if link_row is not None else settings.tutorial_video_link

    en_row = (await session.execute(select(BotSetting).where(BotSetting.key == "tutorial_video_enabled"))).scalar_one_or_none()
    is_enabled = (en_row.value.lower() == "true") if en_row else True
    return current_link, is_enabled


@admin_router.callback_query(F.data == "admin:tutorial")
async def handle_admin_tutorial(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    if session is not None:
        video_link, is_enabled = await _get_tutorial_state(session)
    else:
        async with AsyncSessionLocal() as s:
            video_link, is_enabled = await _get_tutorial_state(s)

    status_badge = "🟢 <b>Enabled</b>" if is_enabled else "🔴 <b>Disabled</b>"
    link_display = f"<code>{html.escape(video_link)}</code>" if video_link else "<i>Not set (empty)</i>"

    text = (
        f"📺 <b>Tutorial Manager</b> (Guide Video)\n\n"
        f"• Status: {status_badge}\n"
        f"• Current Video Link: {link_display}\n\n"
        f"ℹ️ <i>Use the controls below to update the URL, toggle visibility, or remove the link.</i>"
    )
    kb = admin_tutorial_mgmt_kb(video_link=video_link, is_enabled=is_enabled, lang="en")
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data == "admin:tutorial_edit_url")
async def handle_admin_tutorial_edit_url(callback: CallbackQuery, state: FSMContext):
    if not await is_authorized_admin(callback):
        return

    await state.set_state(AdminTutorialFSM.waiting_for_url)
    text = (
        "✏️ <b>Enter New Guide Video URL</b>\n\n"
        "Please type or paste the new video link (must start with <code>http://</code> or <code>https://</code>):\n"
        "Example: <code>https://t.me/voiceclonepro/tutorial</code>"
    )
    kb = admin_tutorial_cancel_kb("en")
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.message(AdminTutorialFSM.waiting_for_url)
async def handle_admin_tutorial_url_input(message: Message, state: FSMContext):
    if not settings.is_admin(message.from_user.id if message.from_user else None):
        return

    url = (message.text or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")) or len(url) < 10:
        await message.answer(
            "⚠️ <b>Invalid URL format!</b>\n"
            "The link must start with <code>http://</code> or <code>https://</code>.\n"
            "Please try again or tap cancel below:",
            reply_markup=admin_tutorial_cancel_kb("en"),
            parse_mode="HTML",
        )
        return

    await state.clear()
    async with AsyncSessionLocal() as session:
        admin_user, _ = await get_or_create_user(session, message.from_user.id)
        lang = admin_user.language_code if admin_user else "my"
        stmt = select(BotSetting).where(BotSetting.key == "tutorial_video_link")
        row = (await session.execute(stmt)).scalar_one_or_none()
        if not row:
            session.add(BotSetting(key="tutorial_video_link", value=url))
        else:
            row.value = url
            row.updated_at = utc_now()

        session.add(AuditLog(
            admin_id=message.from_user.id,
            action="admin.tutorial_update",
            target_type="bot_setting",
            target_id="tutorial_video_link",
            details_json=f'{{"url": "{url}"}}',
        ))
        await session.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📺 Back to Guide Video Mgr", callback_data="admin:tutorial")],
        [InlineKeyboardButton(text=t("btn_admin_dashboard", lang), callback_data="admin:dashboard")],
    ])
    await message.answer(
        f"✅ <b>Guide Video URL Updated!</b>\n\n• New URL: <code>{html.escape(url)}</code>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@admin_router.callback_query(F.data == "admin:tutorial_toggle")
async def handle_admin_tutorial_toggle(callback: CallbackQuery):
    if not await is_authorized_admin(callback):
        return

    async with AsyncSessionLocal() as session:
        stmt = select(BotSetting).where(BotSetting.key == "tutorial_video_enabled")
        row = (await session.execute(stmt)).scalar_one_or_none()
        current_val = (row.value.lower() == "true") if row else True
        new_val = not current_val
        val_str = "true" if new_val else "false"

        if not row:
            session.add(BotSetting(key="tutorial_video_enabled", value=val_str))
        else:
            row.value = val_str
            row.updated_at = utc_now()

        session.add(AuditLog(
            admin_id=callback.from_user.id,
            action="admin.tutorial_toggle",
            target_type="bot_setting",
            target_id="tutorial_video_enabled",
            details_json=f'{{"enabled": {str(new_val).lower()}}}',
        ))
        await session.commit()

    status_str = "Enabled" if new_val else "Disabled"
    await callback.answer(f"Guide video {status_str}!", show_alert=False)
    await handle_admin_tutorial(callback)


@admin_router.callback_query(F.data == "admin:tutorial_remove_prompt")
async def handle_admin_tutorial_remove_prompt(callback: CallbackQuery):
    if not await is_authorized_admin(callback):
        return

    text = (
        "⚠️ <b>Confirm Video Link Removal</b>\n\n"
        "Are you sure you want to remove the guide video URL?\n"
        "Users accessing the Tutorial menu will see a notice that the guide is temporarily unavailable."
    )
    kb = admin_tutorial_confirm_remove_kb("en")
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data == "admin:tutorial_remove_confirm")
async def handle_admin_tutorial_remove_confirm(callback: CallbackQuery):
    if not await is_authorized_admin(callback):
        return

    async with AsyncSessionLocal() as session:
        stmt = select(BotSetting).where(BotSetting.key == "tutorial_video_link")
        row = (await session.execute(stmt)).scalar_one_or_none()
        if not row:
            session.add(BotSetting(key="tutorial_video_link", value=""))
        else:
            row.value = ""
            row.updated_at = utc_now()

        session.add(AuditLog(
            admin_id=callback.from_user.id,
            action="admin.tutorial_remove",
            target_type="bot_setting",
            target_id="tutorial_video_link",
            details_json='{"action": "removed"}',
        ))
        await session.commit()

    await callback.answer("Guide video link removed!", show_alert=False)
    await handle_admin_tutorial(callback)


# ---------------------------------------------------------------------------
# Audit Log Handlers
# ---------------------------------------------------------------------------

@admin_router.callback_query(F.data.startswith("admin:audit"))
async def handle_admin_audit_logs(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    if session is not None:
        admin_user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = admin_user.language_code if admin_user else "my"
        logs = list((await session.execute(
            select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(10)
        )).scalars().all())
    else:
        async with AsyncSessionLocal() as s:
            admin_user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = admin_user.language_code if admin_user else "my"
            logs = list((await s.execute(
                select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(10)
            )).scalars().all())

    text = "📜 <b>Recent Admin Audit Logs (Latest 10)</b>\n\n"
    if logs:
        for log in logs:
            ts_str = log.created_at.strftime("%Y-%m-%d %H:%M UTC") if log.created_at else "Unknown"
            safe_action = html.escape(log.action)
            safe_target = f"{log.target_type}:{log.target_id}" if log.target_type else "system"
            safe_details = html.escape((log.details_json or "")[:60])
            text += f"• <code>{ts_str}</code> | Admin <code>{log.admin_id}</code>\n  <b>{safe_action}</b> on <code>{safe_target}</code>\n  <i>{safe_details}</i>\n\n"
    else:
        text += "<i>No audit logs recorded yet.</i>\n"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Refresh", callback_data="admin:audit")],
            [InlineKeyboardButton(text=t("btn_admin_dashboard", lang), callback_data="admin:dashboard")],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# ---------------------------------------------------------------------------
# Broadcast Handlers
# ---------------------------------------------------------------------------

@admin_router.message(Command("broadcast"))
async def handle_admin_broadcast_command(
    message: Message,
    state: FSMContext,
    session: AsyncSession | None = None,
):
    """Entry point for /broadcast command."""
    if not await is_authorized_admin(message):
        return

    telegram_id = message.from_user.id
    if session is not None:
        user, _ = await get_or_create_user(session, telegram_id)
        lang = user.language_code
        reg_count = len(await get_broadcast_recipient_ids(session, BroadcastRecipientGroup.REGISTERED_ALL))
        imp_all_count = len(await get_broadcast_recipient_ids(session, BroadcastRecipientGroup.IMPORTED_ALL))
        imp_unreg_count = len(await get_broadcast_recipient_ids(session, BroadcastRecipientGroup.IMPORTED_UNREGISTERED))
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, telegram_id)
            lang = user.language_code
            reg_count = len(await get_broadcast_recipient_ids(s, BroadcastRecipientGroup.REGISTERED_ALL))
            imp_all_count = len(await get_broadcast_recipient_ids(s, BroadcastRecipientGroup.IMPORTED_ALL))
            imp_unreg_count = len(await get_broadcast_recipient_ids(s, BroadcastRecipientGroup.IMPORTED_UNREGISTERED))

    await state.set_state(AdminBroadcastFSM.choosing_audience)
    await state.update_data(lang=lang)

    text = (
        "📢 <b>Broadcast Center — Audience Selection</b>\n\n"
        "Please select the target recipient group for your broadcast:\n\n"
        f"• <b>Registered Users:</b> {reg_count} recipients (Active bot users)\n"
        f"• <b>Imported All:</b> {imp_all_count} recipients (Legacy migrated audience)\n"
        f"• <b>Imported Unregistered:</b> {imp_unreg_count} recipients (Legacy users who haven't started V2)\n"
    )
    kb = admin_broadcast_audience_kb(
        registered_count=reg_count,
        imported_all_count=imp_all_count,
        imported_unregistered_count=imp_unreg_count,
        lang=lang,
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@admin_router.callback_query(F.data == "admin:broadcast")
async def handle_admin_broadcast_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession | None = None,
):
    """Entry point from Admin Dashboard Broadcast button."""
    if not await is_authorized_admin(callback):
        return

    telegram_id = callback.from_user.id
    if session is not None:
        user, _ = await get_or_create_user(session, telegram_id)
        lang = user.language_code
        reg_count = len(await get_broadcast_recipient_ids(session, BroadcastRecipientGroup.REGISTERED_ALL))
        imp_all_count = len(await get_broadcast_recipient_ids(session, BroadcastRecipientGroup.IMPORTED_ALL))
        imp_unreg_count = len(await get_broadcast_recipient_ids(session, BroadcastRecipientGroup.IMPORTED_UNREGISTERED))
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, telegram_id)
            lang = user.language_code
            reg_count = len(await get_broadcast_recipient_ids(s, BroadcastRecipientGroup.REGISTERED_ALL))
            imp_all_count = len(await get_broadcast_recipient_ids(s, BroadcastRecipientGroup.IMPORTED_ALL))
            imp_unreg_count = len(await get_broadcast_recipient_ids(s, BroadcastRecipientGroup.IMPORTED_UNREGISTERED))

    await state.set_state(AdminBroadcastFSM.choosing_audience)
    await state.update_data(lang=lang)

    text = (
        "📢 <b>Broadcast Center — Audience Selection</b>\n\n"
        "Please select the target recipient group for your broadcast:\n\n"
        f"• <b>Registered Users:</b> {reg_count} recipients (Active bot users)\n"
        f"• <b>Imported All:</b> {imp_all_count} recipients (Legacy migrated audience)\n"
        f"• <b>Imported Unregistered:</b> {imp_unreg_count} recipients (Legacy users who haven't started V2)\n"
    )
    kb = admin_broadcast_audience_kb(
        registered_count=reg_count,
        imported_all_count=imp_all_count,
        imported_unregistered_count=imp_unreg_count,
        lang=lang,
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:bc_group:"))
async def handle_admin_broadcast_group_select(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession | None = None,
):
    """Handle audience group selection and prompt for broadcast message."""
    if not await is_authorized_admin(callback):
        return

    group_str = callback.data.split("admin:bc_group:")[1]
    data = await state.get_data()
    lang = data.get("lang", "en")

    if session is not None:
        recipients = await get_broadcast_recipient_ids(session, group_str)
    else:
        async with AsyncSessionLocal() as s:
            recipients = await get_broadcast_recipient_ids(s, group_str)

    recipient_count = len(recipients)
    await state.update_data(target_group=group_str, recipient_count=recipient_count)
    await state.set_state(AdminBroadcastFSM.waiting_for_message)

    text = (
        f"📢 <b>Broadcast Center — Compose Message</b>\n\n"
        f"• Target Group: <b>{group_str}</b>\n"
        f"• Total Target Recipients: <b>{recipient_count}</b>\n\n"
        f"Please reply with the broadcast text you want to send:"
    )
    kb = admin_broadcast_prompt_kb(lang=lang)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.message(AdminBroadcastFSM.waiting_for_message)
async def handle_admin_broadcast_message_input(
    message: Message,
    state: FSMContext,
    session: AsyncSession | None = None,
):
    """Handle broadcast text compose and display safe preview."""
    if not await is_authorized_admin(message):
        return

    data = await state.get_data()
    lang = data.get("lang")
    target_group = data.get("target_group", BroadcastRecipientGroup.REGISTERED_ALL.value)
    if not lang:
        telegram_id = message.from_user.id
        if session is not None:
            user, _ = await get_or_create_user(session, telegram_id)
            lang = user.language_code
        else:
            async with AsyncSessionLocal() as s:
                user, _ = await get_or_create_user(s, telegram_id)
                lang = user.language_code

    broadcast_text = (message.text or "").strip()
    if not broadcast_text:
        await message.answer(t("admin_broadcast_empty_text", lang))
        return

    if session is not None:
        recipients = await get_broadcast_recipient_ids(session, target_group)
    else:
        async with AsyncSessionLocal() as s:
            recipients = await get_broadcast_recipient_ids(s, target_group)

    recipient_count = len(recipients)
    token = uuid.uuid4().hex[:12]

    await state.update_data(
        broadcast_text=broadcast_text,
        token=token,
        recipient_count=recipient_count,
        target_group=target_group,
        lang=lang,
    )
    await state.set_state(AdminBroadcastFSM.confirming)

    safe_preview = html.escape(broadcast_text)
    preview_text = (
        f"📢 <b>Broadcast Preview & Confirmation</b>\n\n"
        f"• Target Group: <b>{target_group}</b>\n"
        f"• Target Recipients: <b>{recipient_count} users</b>\n\n"
        f"📝 <b>Message Content:</b>\n"
        f"<blockquote>{safe_preview}</blockquote>\n\n"
        f"⚠️ Are you sure you want to broadcast this message to {recipient_count} recipients?"
    )
    await message.answer(
        preview_text,
        reply_markup=admin_broadcast_preview_kb(token, lang),
        parse_mode="HTML",
    )


@admin_router.callback_query(F.data == "admin:broadcast_cancel")
async def handle_admin_broadcast_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession | None = None,
):
    """Cancel broadcast composing/confirmation and return to dashboard."""
    if not await is_authorized_admin(callback):
        return

    data = await state.get_data()
    lang = data.get("lang")
    if not lang:
        telegram_id = callback.from_user.id
        if session is not None:
            user, _ = await get_or_create_user(session, telegram_id)
            lang = user.language_code
        else:
            async with AsyncSessionLocal() as s:
                user, _ = await get_or_create_user(s, telegram_id)
                lang = user.language_code

    await state.clear()
    await callback.answer(t("admin_alert_broadcast_cancelled", lang), show_alert=True)

    telegram_id = callback.from_user.id
    if session is not None:
        text, kb = await render_admin_dashboard(telegram_id, lang=lang, session=session)
    else:
        async with AsyncSessionLocal() as s:
            text, kb = await render_admin_dashboard(telegram_id, lang=lang, session=s)

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")


@admin_router.callback_query(F.data.startswith("admin:broadcast_send:"))
async def handle_admin_broadcast_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession | None = None,
):
    """Validate confirmation nonce, clear state to prevent duplicates, and run broadcast."""
    if not await is_authorized_admin(callback):
        return

    token = callback.data.split("admin:broadcast_send:")[1]
    data = await state.get_data()
    expected_token = data.get("token")
    broadcast_text = data.get("broadcast_text")
    target_group = data.get("target_group", BroadcastRecipientGroup.REGISTERED_ALL.value)
    lang = data.get("lang")
    if not lang:
        telegram_id = callback.from_user.id
        if session is not None:
            user, _ = await get_or_create_user(session, telegram_id)
            lang = user.language_code
        else:
            async with AsyncSessionLocal() as s:
                user, _ = await get_or_create_user(s, telegram_id)
                lang = user.language_code

    if not expected_token or expected_token != token or not broadcast_text:
        await callback.answer(t("admin_alert_broadcast_invalid_or_expired", lang), show_alert=True)
        return

    # Clear state immediately to prevent duplicate sends / double-clicks
    await state.clear()
    await callback.answer()

    if session is not None:
        recipient_ids = await get_broadcast_recipient_ids(session, target_group)
        campaign = await create_or_get_broadcast_campaign(
            session=session,
            admin_id=callback.from_user.id,
            token=token,
            text=broadcast_text,
            recipient_ids=recipient_ids,
        )
        total_recipients = campaign.total_recipients
        campaign_id = campaign.id
        session.add(AuditLog(
            admin_id=callback.from_user.id,
            action="broadcast.send",
            target_type="broadcast_campaign",
            target_id=str(campaign_id),
            details_json=json.dumps({
                "target_group": target_group,
                "total_recipients": total_recipients,
                "token": token,
            }),
        ))
        await session.commit()
    else:
        async with AsyncSessionLocal() as s:
            recipient_ids = await get_broadcast_recipient_ids(s, target_group)
            campaign = await create_or_get_broadcast_campaign(
                session=s,
                admin_id=callback.from_user.id,
                token=token,
                text=broadcast_text,
                recipient_ids=recipient_ids,
            )
            total_recipients = campaign.total_recipients
            campaign_id = campaign.id
            s.add(AuditLog(
                admin_id=callback.from_user.id,
                action="broadcast.send",
                target_type="broadcast_campaign",
                target_id=str(campaign_id),
                details_json=json.dumps({
                    "target_group": target_group,
                    "total_recipients": total_recipients,
                    "token": token,
                }),
            ))
            await s.commit()

    # Initial progress notice
    initial_text = t(
        "admin_broadcast_in_progress",
        lang,
        sent=0,
        total=total_recipients,
        failed=0,
    )
    try:
        await callback.message.edit_text(initial_text, parse_mode="HTML")
    except Exception:
        pass

    async def update_progress(sent_count: int, failed_count: int, skipped_count: int):
        try:
            await callback.message.edit_text(
                t(
                    "admin_broadcast_in_progress",
                    lang,
                    sent=sent_count,
                    total=total_recipients,
                    failed=failed_count + skipped_count,
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

    # Execute broadcast delivery with durable tracking
    if session is not None:
        result = await deliver_broadcast(
            bot=callback.bot,
            recipient_ids=recipient_ids,
            text=broadcast_text,
            progress_callback=update_progress,
            session=session,
            campaign_id=campaign_id,
        )
    else:
        async with AsyncSessionLocal() as s:
            result = await deliver_broadcast(
                bot=callback.bot,
                recipient_ids=recipient_ids,
                text=broadcast_text,
                progress_callback=update_progress,
                session=s,
                campaign_id=campaign_id,
            )

    completed_text = t(
        "admin_broadcast_completed",
        lang,
        total=result.total,
        sent=result.sent,
        failed=result.failed,
        skipped=result.skipped,
        duration=f"{result.duration_seconds:.1f}",
    )
    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_admin_dashboard", lang), callback_data="admin:dashboard")],
        ]
    )
    try:
        await callback.message.edit_text(completed_text, reply_markup=back_kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(completed_text, reply_markup=back_kb, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Custom Voice Request (CVR) Admin Handlers
# ---------------------------------------------------------------------------

@admin_router.callback_query(F.data == "admin:cvr")
@admin_router.callback_query(F.data.startswith("admin:cvr:filter:"))
async def handle_admin_cvr_list(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    filter_val = "PENDING"
    if callback.data.startswith("admin:cvr:filter:"):
        filter_val = callback.data.split(":")[3]

    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        status_arg = CustomVoiceRequestStatus.PENDING.value if filter_val == "PENDING" else None
        requests = await get_custom_voice_requests(session, status=status_arg)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            status_arg = CustomVoiceRequestStatus.PENDING.value if filter_val == "PENDING" else None
            requests = await get_custom_voice_requests(s, status=status_arg)

    filter_label = "Pending Requests" if filter_val == "PENDING" else "All Requests"
    text = (
        f"👑 <b>Custom Voice Requests ({filter_label})</b>\n\n"
        f"Total: <b>{len(requests)}</b>\n\n"
        f"<i>Click a request to view details, hear audio sample, or assign model:</i>"
    )
    try:
        await callback.message.edit_text(
            text,
            reply_markup=admin_cvr_list_kb(requests, status_filter=filter_val, lang=lang),
            parse_mode="HTML",
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            await callback.message.answer(
                text,
                reply_markup=admin_cvr_list_kb(requests, status_filter=filter_val, lang=lang),
                parse_mode="HTML",
            )
    try:
        await callback.answer()
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("admin:cvr:detail:"))
async def handle_admin_cvr_detail(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    req_id = callback.data.split(":")[3]

    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        req = await get_custom_voice_request_by_id(session, req_id)
        assigned_model_name = "None"
        if req and req.assigned_model_id:
            m = await session.get(VoiceModel, req.assigned_model_id)
            if m:
                assigned_model_name = m.name
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            req = await get_custom_voice_request_by_id(s, req_id)
            assigned_model_name = "None"
            if req and req.assigned_model_id:
                m = await s.get(VoiceModel, req.assigned_model_id)
                if m:
                    assigned_model_name = m.name

    if not req:
        await callback.answer("Request not found!", show_alert=True)
        return

    st_icon = "⏳ Pending" if req.status == "PENDING" else ("✅ Approved" if req.status == "APPROVED" else "❌ Rejected")
    notes_str = f"\n• Admin Notes: <i>{html.escape(req.admin_notes)}</i>" if req.admin_notes else ""
    text = (
        f"👑 <b>Custom Voice Request #{req.id}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• User ID: <code>{req.user_id}</code>\n"
        f"• Requested Model: <b>{html.escape(req.model_name)}</b>\n"
        f"• Status: <b>{st_icon}</b>\n"
        f"• File Name: <code>{html.escape(req.sample_file_name or 'N/A')}</code>\n"
        f"• Duration: <b>{req.sample_duration_seconds or 'N/A'}s</b>\n"
        f"• Assigned Model: <b>{html.escape(assigned_model_name)}</b>\n"
        f"• Created At: <code>{req.created_at.strftime('%Y-%m-%d %H:%M:%S')}</code>"
        f"{notes_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Assign existing model from DB, or train in Colab, put files in storage, and click 'Scan & Assign Colab Model'.</i>"
    )
    try:
        await callback.message.edit_text(
            text,
            reply_markup=admin_cvr_detail_kb(req.id, status=req.status, lang=lang),
            parse_mode="HTML",
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            await callback.message.answer(
                text,
                reply_markup=admin_cvr_detail_kb(req.id, status=req.status, lang=lang),
                parse_mode="HTML",
            )
    try:
        await callback.answer()
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("admin:cvr:play:"))
async def handle_admin_cvr_play(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    req_id = callback.data.split(":")[3]

    if session is not None:
        req = await get_custom_voice_request_by_id(session, req_id)
    else:
        async with AsyncSessionLocal() as s:
            req = await get_custom_voice_request_by_id(s, req_id)

    if not req or not req.sample_file_id:
        await callback.answer("Audio sample not found!", show_alert=True)
        return

    try:
        caption = f"🎧 Audio Sample for #{req.id} ({req.model_name})"
        if (req.sample_file_name or "").endswith(".ogg"):
            await callback.bot.send_voice(callback.from_user.id, req.sample_file_id, caption=caption)
        else:
            await callback.bot.send_audio(callback.from_user.id, req.sample_file_id, caption=caption)
        await callback.answer("Audio sent to your chat!")
    except Exception as e:
        logger.warning("Failed to play audio sample: %s", e)
        try:
            await callback.bot.send_document(
                callback.from_user.id,
                req.sample_file_id,
                caption=f"🎧 Audio Sample for #{req.id} ({req.model_name})",
            )
            await callback.answer("Audio sent as document!")
        except Exception as ex:
            await callback.answer(f"Failed to load audio: {ex}", show_alert=True)


@admin_router.callback_query(F.data.startswith("admin:cvr:assign_exist:"))
async def handle_admin_cvr_assign_exist(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    req_id = callback.data.split(":")[3]

    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        req = await get_custom_voice_request_by_id(session, req_id)
        models = list((await session.execute(
            select(VoiceModel).where(VoiceModel.status == VoiceModelStatus.ACTIVE.value).order_by(VoiceModel.name)
        )).scalars().all())
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            req = await get_custom_voice_request_by_id(s, req_id)
            models = list((await s.execute(
                select(VoiceModel).where(VoiceModel.status == VoiceModelStatus.ACTIVE.value).order_by(VoiceModel.name)
            )).scalars().all())

    if not req:
        await callback.answer("Request not found!", show_alert=True)
        return

    text = (
        f"🔄 <b>Select Existing Model for #{req.id} ({html.escape(req.model_name)})</b>\n\n"
        f"Select any existing trained model to grant immediate access to User <code>{req.user_id}</code> without retraining:"
    )
    await callback.message.edit_text(
        text,
        reply_markup=admin_cvr_select_model_kb(req.id, models, lang=lang),
        parse_mode="HTML",
    )
    try:
        await callback.answer()
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("admin:cvr:scan_assign:"))
async def handle_admin_cvr_scan_assign(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    req_id = callback.data.split(":")[3]

    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        req = await get_custom_voice_request_by_id(session, req_id)
        scanned = await scan_all_models(session)
        await session.commit()
        models = list((await session.execute(
            select(VoiceModel).where(VoiceModel.status == VoiceModelStatus.ACTIVE.value).order_by(VoiceModel.name)
        )).scalars().all())
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            req = await get_custom_voice_request_by_id(s, req_id)
            scanned = await scan_all_models(s)
            await s.commit()
            models = list((await s.execute(
                select(VoiceModel).where(VoiceModel.status == VoiceModelStatus.ACTIVE.value).order_by(VoiceModel.name)
            )).scalars().all())

    if not req:
        await callback.answer("Request not found!", show_alert=True)
        return

    toast = f"Scanned storage: {len(scanned)} model(s) registered/updated!"
    try:
        await callback.answer(toast, show_alert=False)
    except Exception:
        pass

    text = (
        f"📁 <b>Scanned Models for #{req.id} ({html.escape(req.model_name)})</b>\n\n"
        f"Storage scanned! Select the model to assign to User <code>{req.user_id}</code>:"
    )
    await callback.message.edit_text(
        text,
        reply_markup=admin_cvr_select_model_kb(req.id, models, lang=lang),
        parse_mode="HTML",
    )


@admin_router.callback_query(F.data.startswith("admin:cvr:do_assign:"))
async def handle_admin_cvr_do_assign(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    parts = callback.data.split(":")
    req_id = parts[3]
    model_id = int(parts[4])

    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        success, req, err = await assign_model_to_request(
            session=session,
            request_id=req_id,
            model_id=model_id,
            admin_id=callback.from_user.id,
        )
        if success and req:
            model = await session.get(VoiceModel, model_id)
            target_user = await session.get(User, req.user_id)
            user_lang = target_user.language_code if target_user else "my"
            await session.commit()
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            success, req, err = await assign_model_to_request(
                session=s,
                request_id=req_id,
                model_id=model_id,
                admin_id=callback.from_user.id,
            )
            if success and req:
                model = await s.get(VoiceModel, model_id)
                target_user = await s.get(User, req.user_id)
                user_lang = target_user.language_code if target_user else "my"
                await s.commit()

    if not success or not req:
        await callback.answer(f"Failed to assign model: {err}", show_alert=True)
        return

    # Notify user that their model is active
    try:
        notice = t("custom_voice_user_approved_notice", user_lang, model_name=model.name if model else "Custom Voice")
        await callback.bot.send_message(req.user_id, notice, parse_mode="HTML")
    except Exception as e:
        logger.warning("Failed to send approval notice to user %s: %s", req.user_id, e)

    await callback.answer(f"✅ Assigned model to #{req.id} and notified user!", show_alert=True)
    await handle_admin_cvr_detail(callback, session=session)


@admin_router.callback_query(F.data.startswith("admin:cvr:reject_prompt:"))
async def handle_admin_cvr_reject_prompt(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    req_id = callback.data.split(":")[3]

    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        req = await get_custom_voice_request_by_id(session, req_id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            req = await get_custom_voice_request_by_id(s, req_id)

    if not req:
        await callback.answer("Request not found!", show_alert=True)
        return

    text = (
        f"❌ <b>Reject Request #{req.id}</b>\n\n"
        f"Select a preset rejection reason or specify custom reason. This will release the user's slot and inform them:"
    )
    await callback.message.edit_text(
        text,
        reply_markup=admin_cvr_reject_presets_kb(req.id, lang=lang),
        parse_mode="HTML",
    )
    try:
        await callback.answer()
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("admin:cvr:do_reject:"))
async def handle_admin_cvr_do_reject(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return
    parts = callback.data.split(":")
    req_id = parts[3]
    idx = int(parts[4])

    reasons = [
        "Audio sample quality is too noisy or unclear. Please provide a clean voice recording.",
        "Audio contains background music. Please provide voice-only audio.",
        "Audio duration is too short (< 30 seconds). Please provide at least 30s-1m.",
    ]
    reason = reasons[idx] if idx < len(reasons) else "Audio sample could not be trained."

    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        success, req, err = await reject_custom_voice_request(
            session=session,
            request_id=req_id,
            admin_id=callback.from_user.id,
            reason=reason,
        )
        if success and req:
            target_user = await session.get(User, req.user_id)
            user_lang = target_user.language_code if target_user else "my"
            await session.commit()
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            success, req, err = await reject_custom_voice_request(
                session=s,
                request_id=req_id,
                admin_id=callback.from_user.id,
                reason=reason,
            )
            if success and req:
                target_user = await s.get(User, req.user_id)
                user_lang = target_user.language_code if target_user else "my"
                await s.commit()

    if not success or not req:
        await callback.answer(f"Failed to reject request: {err}", show_alert=True)
        return

    try:
        notice = t("custom_voice_user_rejected_notice", user_lang, model_name=req.model_name, reason=reason)
        await callback.bot.send_message(req.user_id, notice, parse_mode="HTML")
    except Exception as e:
        logger.warning("Failed to send rejection notice to user %s: %s", req.user_id, e)

    await callback.answer("❌ Request rejected and user notified.", show_alert=True)
    await handle_admin_cvr_detail(callback, session=session)


@admin_router.callback_query(F.data.startswith("admin:cvr:reject_custom:"))
async def handle_admin_cvr_reject_custom(callback: CallbackQuery, state: FSMContext):
    if not await is_authorized_admin(callback):
        return
    req_id = callback.data.split(":")[3]
    await state.set_state(AdminCVRRejectFSM.waiting_for_reason)
    await state.update_data(cvr_req_id=req_id)

    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data=f"admin:cvr:detail:{req_id}")]]
    )
    await callback.message.edit_text(
        f"✍️ <b>Custom Rejection Reason for #{req_id}</b>\n\nPlease send the rejection reason text to inform the user:",
        reply_markup=cancel_kb,
        parse_mode="HTML",
    )
    try:
        await callback.answer()
    except Exception:
        pass


@admin_router.message(AdminCVRRejectFSM.waiting_for_reason, F.text)
async def handle_admin_cvr_custom_reason_text(message: Message, state: FSMContext):
    if not await is_authorized_admin(message):
        return
    reason = message.text.strip()
    data = await state.get_data()
    req_id = data.get("cvr_req_id")
    await state.clear()

    if not req_id:
        await message.answer("Error: Request ID lost.")
        return

    async with AsyncSessionLocal() as session:
        success, req, err = await reject_custom_voice_request(
            session=session,
            request_id=req_id,
            admin_id=message.from_user.id,
            reason=reason,
        )
        if success and req:
            target_user = await session.get(User, req.user_id)
            user_lang = target_user.language_code if target_user else "my"
            await session.commit()

    if not success or not req:
        await message.answer(f"❌ Failed to reject request: {err}")
        return

    try:
        notice = t("custom_voice_user_rejected_notice", user_lang, model_name=req.model_name, reason=reason)
        await message.bot.send_message(req.user_id, notice, parse_mode="HTML")
    except Exception as e:
        logger.warning("Failed to send rejection notice to user %s: %s", req.user_id, e)

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to CVR", callback_data="admin:cvr")]]
    )
    await message.answer(
        f"❌ Request #{req.id} rejected and user notified.\n\nReason: <i>{html.escape(reason)}</i>",
        reply_markup=back_kb,
        parse_mode="HTML",
    )

