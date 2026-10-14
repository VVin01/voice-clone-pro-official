from __future__ import annotations

import html
import logging
from pathlib import Path
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import uuid
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.models.enums import (
    AccountCategory,
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
    Job,
    MinuteBalance,
    MinuteLedgerEvent,
    PaymentRequest,
    Referral,
    ReferralReward,
    User,
    VoiceModel,
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
)
from app.services.payments import approve_payment_request, reject_payment_request
from app.ui.keyboards import (
    admin_adjust_cancel_kb,
    admin_adjust_preview_kb,
    admin_broadcast_preview_kb,
    admin_broadcast_prompt_kb,
    admin_dashboard_kb,
    admin_queue_failures_kb,
    admin_queue_kb,
    admin_user_card_kb,
    admin_user_search_cancel_kb,
    admin_users_list_kb,
)

logger = logging.getLogger(__name__)
admin_router = Router(name="admin")


class AdminBroadcastFSM(StatesGroup):
    waiting_for_message = State()
    confirming = State()


class AdminAdjustFSM(StatesGroup):
    waiting_for_amount = State()
    waiting_for_reason = State()
    confirming = State()


class AdminUserSearchFSM(StatesGroup):
    waiting_for_query = State()


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
    kb = admin_dashboard_kb(lang=lang, pending_payments=data["pending_payments"])
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

    buttons = [
        [
            InlineKeyboardButton(
                text=f"{badge(m)} {m.name} ({m.status}){' ⭐' if m.is_default_free else ''}",
                callback_data=f"admin:model:{m.id}",
            )
        ]
        for m in models
    ]
    buttons.append([InlineKeyboardButton(text=t("admin_btn_scan_models", lang), callback_data="admin:model_scan")])
    buttons.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="admin:dashboard")])

    text = t("admin_voice_mgmt_title", lang)
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
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

    await callback.answer(t("admin_alert_scanned", lang, count=len(scanned)), show_alert=True)
    await handle_admin_voices(callback)


async def render_admin_model_detail(
    callback: CallbackQuery,
    model_id: int,
    session: AsyncSession | None = None,
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
    text = t(
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

    buttons = []
    if model.status == VoiceModelStatus.ACTIVE.value:
        buttons.append([InlineKeyboardButton(text=t("admin_btn_disable", lang), callback_data=f"admin:mod_toggle:{model.id}:disable")])
        if model.type == VoiceModelType.SYSTEM.value:
            free_label = t("admin_btn_unset_free", lang) if model.is_default_free else t("admin_btn_set_free", lang)
            buttons.append([InlineKeyboardButton(text=free_label, callback_data=f"admin:mod_free:{model.id}")])
    else:
        buttons.append([InlineKeyboardButton(text=t("admin_btn_activate", lang), callback_data=f"admin:mod_toggle:{model.id}:activate")])

    buttons.append([InlineKeyboardButton(text=t("admin_btn_back_to_models", lang), callback_data="admin:voices")])

    try:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


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

    await callback.answer(t("admin_alert_model_toggled", lang, action=action, msg=msg), show_alert=True)
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

    await callback.answer(t("admin_alert_free_updated", lang, msg=msg), show_alert=True)
    await render_admin_model_detail(callback, model_id=model_id, session=session)


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

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:pay_detail:"))
async def handle_admin_pay_detail(callback: CallbackQuery):
    if not await is_authorized_admin(callback):
        return

    pay_id = callback.data.split(":")[2]
    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        payment = await session.get(PaymentRequest, pay_id)

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
            InlineKeyboardButton(text=t("admin_btn_pay_approve_credit", lang), callback_data=f"admin:pay_approve:{payment.id}"),
            InlineKeyboardButton(text=t("btn_admin_pay_reject", lang), callback_data=f"admin:pay_reject:{payment.id}"),
        ])
    buttons.append([InlineKeyboardButton(text=t("admin_btn_back_to_payments", lang), callback_data="admin:payments")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


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
async def handle_admin_pay_approve(callback: CallbackQuery):
    if not await is_authorized_admin(callback):
        return

    pay_id = callback.data.split(":")[2]
    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        ok, payment, msg = await approve_payment_request(session, pay_id, admin_id=callback.from_user.id)
        if ok and payment:
            await session.commit()
            # Notify user about approval
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

    await callback.answer(t("admin_alert_payment_approved", lang, msg=msg), show_alert=True)
    await handle_admin_payments(callback)


@admin_router.callback_query(F.data.startswith("admin:pay_reject:"))
async def handle_admin_pay_reject(callback: CallbackQuery):
    if not await is_authorized_admin(callback):
        return

    pay_id = callback.data.split(":")[2]
    async with AsyncSessionLocal() as session:
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

    await callback.answer(t("admin_alert_payment_rejected", lang, msg=msg), show_alert=True)
    await handle_admin_payments(callback)


# ---------------------------------------------------------------------------
# Queue Monitor & Users Handlers
# ---------------------------------------------------------------------------

@admin_router.callback_query(F.data == "admin:queue")
async def handle_admin_queue(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        queued = (await session.execute(select(func.count()).where(Job.status == JobStatus.QUEUED.value))).scalar() or 0
        running = (await session.execute(select(func.count()).where(Job.status == JobStatus.RUNNING.value))).scalar() or 0
        success = (await session.execute(select(func.count()).where(Job.status == JobStatus.SUCCESS.value))).scalar() or 0
        failed = (await session.execute(select(func.count()).where(Job.status == JobStatus.FAILED.value))).scalar() or 0
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            queued = (await s.execute(select(func.count()).where(Job.status == JobStatus.QUEUED.value))).scalar() or 0
            running = (await s.execute(select(func.count()).where(Job.status == JobStatus.RUNNING.value))).scalar() or 0
            success = (await s.execute(select(func.count()).where(Job.status == JobStatus.SUCCESS.value))).scalar() or 0
            failed = (await s.execute(select(func.count()).where(Job.status == JobStatus.FAILED.value))).scalar() or 0

    text = t("admin_queue_monitor", lang, queued=queued, running=running, success=success, failed=failed)
    kb = admin_queue_kb(lang=lang, failed_count=failed)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data == "admin:queue_failures")
async def handle_admin_queue_failures(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    if session is not None:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        failed_jobs = (await session.execute(
            select(Job).where(Job.status == JobStatus.FAILED.value).order_by(Job.created_at.desc()).limit(5)
        )).scalars().all()
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            failed_jobs = (await s.execute(
                select(Job).where(Job.status == JobStatus.FAILED.value).order_by(Job.created_at.desc()).limit(5)
            )).scalars().all()

    if not failed_jobs:
        text = t("admin_queue_failures_empty", lang)
    else:
        lines = [t("admin_queue_failures_title", lang, count=len(failed_jobs))]
        for j in failed_jobs:
            ts_str = j.created_at.strftime("%Y-%m-%d %H:%M UTC") if j.created_at else "Unknown"
            safe_err = html.escape(str(j.error_message or "Unknown error")[:120])
            lines.append(f"• Job <code>#{j.id}</code> (User <code>{j.user_id}</code>) at <code>{ts_str}</code>\n  <i>{safe_err}</i>\n")
        text = "\n".join(lines)

    kb = admin_queue_failures_kb(lang=lang)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data.in_(("admin:queue_recover_prompt", "admin:queue_recover_confirm")))
async def handle_admin_queue_recover_disabled(callback: CallbackQuery):
    if not await is_authorized_admin(callback):
        return
    await callback.answer(
        "⚠️ Interactive queue recovery is disabled for safety. Orphaned jobs are recovered automatically at worker startup.",
        show_alert=True,
    )


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

    await callback.answer(t("admin_alert_user_status_updated", lang, status=new_status), show_alert=True)
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
    await callback.answer(f"Adjusted {minutes} minutes successfully!", show_alert=True)


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

    from app.services.payments import get_active_pricing_plans
    if session is not None:
        plans = await get_active_pricing_plans(session)
    else:
        async with AsyncSessionLocal() as s:
            plans = await get_active_pricing_plans(s)

    lines = [t("admin_pricing_title", "en")]
    for p in plans:
        lines.append(t("admin_pricing_item", "en", code=p.code, minutes=p.minutes, price_mmk=p.price_mmk))

    lines.append(t("admin_pricing_footnote", "en"))
    buttons = [
        [InlineKeyboardButton(text=t("admin_btn_back_to_payments", "en"), callback_data="admin:payments")],
        [InlineKeyboardButton(text=t("admin_title", "en"), callback_data="admin:dashboard")],
    ]
    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data == "admin:settings")
async def handle_admin_settings(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    text = t(
        "admin_settings_text",
        "en",
        app_env=settings.app_env,
        app_name=settings.app_name,
        log_level=settings.log_level,
        char_limit_free=settings.character_limit_free,
        char_limit_purchased=settings.character_limit_purchased,
        default_tts_voice=settings.default_tts_voice,
        required_channel_id=settings.required_channel_id or "Not Set",
        required_group_id=settings.required_group_id or "Not Set",
        applio_workdir=settings.applio_workdir,
        applio_python=settings.applio_python or "Auto (<workdir>/env/python.exe)",
        applio_runner=settings.applio_runner or "Auto (<root>/worker/applio_runner.py)",
    )
    buttons = [[InlineKeyboardButton(text=t("btn_back", "en"), callback_data="admin:dashboard")]]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data == "admin:tutorial")
async def handle_admin_tutorial(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    text = t("admin_tutorial_text", "en", video_link=settings.tutorial_video_link)
    buttons = [
        [InlineKeyboardButton(text=t("admin_btn_test_tutorial", "en"), url=settings.tutorial_video_link)],
        [InlineKeyboardButton(text=t("btn_back", "en"), callback_data="admin:dashboard")],
    ]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
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
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, telegram_id)
            lang = user.language_code

    await state.set_state(AdminBroadcastFSM.waiting_for_message)
    await state.update_data(lang=lang)
    await message.answer(
        t("admin_broadcast_prompt", lang),
        reply_markup=admin_broadcast_prompt_kb(lang),
        parse_mode="HTML",
    )


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
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, telegram_id)
            lang = user.language_code

    await state.set_state(AdminBroadcastFSM.waiting_for_message)
    await state.update_data(lang=lang)
    try:
        await callback.message.edit_text(
            t("admin_broadcast_prompt", lang),
            reply_markup=admin_broadcast_prompt_kb(lang),
            parse_mode="HTML",
        )
    except Exception:
        await callback.message.answer(
            t("admin_broadcast_prompt", lang),
            reply_markup=admin_broadcast_prompt_kb(lang),
            parse_mode="HTML",
        )
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
        recipients = await get_broadcast_recipient_ids(session)
    else:
        async with AsyncSessionLocal() as s:
            recipients = await get_broadcast_recipient_ids(s)

    recipient_count = len(recipients)
    token = uuid.uuid4().hex[:12]

    await state.update_data(
        broadcast_text=broadcast_text,
        token=token,
        recipient_count=recipient_count,
        lang=lang,
    )
    await state.set_state(AdminBroadcastFSM.confirming)

    safe_preview = html.escape(broadcast_text)
    preview_text = t(
        "admin_broadcast_preview",
        lang,
        recipient_count=recipient_count,
        preview_text=safe_preview,
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
        recipient_ids = await get_broadcast_recipient_ids(session)
        campaign = await create_or_get_broadcast_campaign(
            session=session,
            admin_id=callback.from_user.id,
            token=token,
            text=broadcast_text,
            recipient_ids=recipient_ids,
        )
        total_recipients = campaign.total_recipients
        campaign_id = campaign.id
    else:
        async with AsyncSessionLocal() as s:
            recipient_ids = await get_broadcast_recipient_ids(s)
            campaign = await create_or_get_broadcast_campaign(
                session=s,
                admin_id=callback.from_user.id,
                token=token,
                text=broadcast_text,
                recipient_ids=recipient_ids,
            )
            total_recipients = campaign.total_recipients
            campaign_id = campaign.id

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
            [InlineKeyboardButton(text=t("admin_title", lang), callback_data="admin:dashboard")],
        ]
    )
    try:
        await callback.message.edit_text(completed_text, reply_markup=back_kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(completed_text, reply_markup=back_kb, parse_mode="HTML")
