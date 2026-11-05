from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
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
    BroadcastRecipientGroup,
    JobStatus,
    PaymentStatus,
    UserStatus,
)
from app.models.schema import (
    AuditLog,
    BotSetting,
    Job,
    PaymentRequest,
    User,
)
from app.services.broadcast import (
    create_or_get_broadcast_campaign,
    deliver_broadcast,
    get_broadcast_recipient_ids,
)
from app.services.i18n import t
from app.services.ledger import get_or_create_user
from app.services.payment_review import (
    apply_edited_approval,
    approved_status_line,
    claim_payment_review,
    mark_copies_claimed,
    payment_claim_is_stale,
    rejected_status_line,
    release_payment_claim,
    restore_admin_copies,
    sync_admin_copies,
)
from app.services.payments import approve_payment_request, package_recap_credits, reject_payment_request
from app.ui.keyboards import (
    admin_broadcast_audience_kb,
    admin_broadcast_preview_kb,
    admin_broadcast_prompt_kb,
    admin_menu_kb,
    admin_pay_approve_confirm_kb,
    admin_pay_reject_confirm_kb,
    admin_tutorial_cancel_kb,
    admin_tutorial_confirm_remove_kb,
    admin_tutorial_mgmt_kb,
)
from app.services.dynamic_settings import set_dynamic_setting
from app.services.url_safety import is_valid_telegram_link
from app.ui.helpers import safe_edit_message

logger = logging.getLogger(__name__)
admin_router = Router(name="admin")


def _wrap_admin_value(raw: str, width: int = 56, max_chars: int = 400) -> str:
    """Render a long admin value (error text, JSON, URL) wrapped.

    R40 item 15e: Telegram clients do not wrap unbroken tokens, so
    long values ran off the message edge, and the old ``[:60]`` /
    ``[:120]`` slices cut content mid-word. Wrap word-aware (long
    tokens broken), shorten only at a word boundary when the value is
    very long, escape AFTER wrapping so entities stay intact.
    """
    import textwrap

    text = str(raw or "")
    if len(text) > max_chars:
        text = textwrap.shorten(text, width=max_chars, placeholder=" …")
    wrapped_lines = []
    for line in text.split("\n"):
        wrapped_lines.append(
            textwrap.fill(
                line,
                width=width,
                break_long_words=True,
                break_on_hyphens=False,
            )
            if line
            else ""
        )
    return html.escape("\n".join(wrapped_lines))


class AdminBroadcastFSM(StatesGroup):
    choosing_audience = State()
    waiting_for_message = State()
    confirming = State()


class AdminTutorialFSM(StatesGroup):
    waiting_for_url = State()
    waiting_for_voice_url = State()
    waiting_for_recap_url = State()


async def is_authorized_admin(event: CallbackQuery | Message) -> bool:
    """Enforce strict server-side admin authorization."""
    user_id = event.from_user.id if event.from_user else None
    if not settings.is_admin(user_id):
        logger.warning("Unauthorized admin access attempt by user %s", user_id)
        # Resolve the caller's actual language like neighbouring handlers
        # (user.language_code), instead of forcing English. Callers with no
        # stored profile have no actual language on record, so the previous
        # English text remains the fallback for them only.
        lang = "en"
        if user_id is not None:
            try:
                async with AsyncSessionLocal() as _s:
                    _u = await _s.get(User, user_id)
                    if _u is not None and _u.language_code:
                        lang = _u.language_code
            except Exception:
                pass
        if isinstance(event, CallbackQuery):
            await event.answer(t("admin_not_authorized", lang), show_alert=True)
        else:
            await event.answer(t("admin_not_authorized", lang))
        return False
    return True


_FAILURE_CLASS_RE = re.compile(r"^([A-Z][A-Z0-9_]{2,})")


def _failure_class(error_message: str | None) -> str | None:
    """Short class label for the most recent failed job (pulse line).

    Job errors are stored as free text, often prefixed with a stable
    machine class ("INSUFFICIENT_BALANCE: ...", "MODEL_NOT_AUTHORIZED",
    downloader classifications). Prefer that leading token; otherwise
    fall back to the first line, truncated — HTML-escaped either way,
    because the pulse renders with parse_mode=HTML.
    """
    if not error_message:
        return None
    first_line = str(error_message).strip().splitlines()[0].strip()
    if not first_line:
        return None
    m = _FAILURE_CLASS_RE.match(first_line)
    label = m.group(1) if m else first_line[:48]
    return html.escape(label)


async def _fetch_pulse_metrics(session: AsyncSession) -> dict:
    """The compact read-only pulse (bot cleanup 2026-11-03).

    Replaces the old 11-button dashboard's metric fetch: total users,
    paid users, jobs created today (UTC), live queue counts, and the
    most recent failed job for the failure-class line. All counts come
    from the same tables the dashboard used — nothing invented.
    """
    total_users = (await session.execute(select(func.count()).select_from(User))).scalar() or 0
    purchased_users = (await session.execute(select(func.count()).where(User.account_category == AccountCategory.PURCHASED.value))).scalar() or 0

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    jobs_today = (await session.execute(select(func.count()).where(Job.created_at >= today_start))).scalar() or 0
    queued_jobs = (await session.execute(select(func.count()).where(Job.status == JobStatus.QUEUED.value))).scalar() or 0
    running_jobs = (await session.execute(select(func.count()).where(Job.status == JobStatus.RUNNING.value))).scalar() or 0

    last_failed_job = (await session.execute(
        select(Job).where(Job.status == JobStatus.FAILED.value).order_by(Job.created_at.desc()).limit(1)
    )).scalar_one_or_none()

    return {
        "total_users": total_users,
        "purchased_users": purchased_users,
        "jobs_today": jobs_today,
        "queued_jobs": queued_jobs,
        "running_jobs": running_jobs,
        "last_failed_error": last_failed_job.error_message if last_failed_job else None,
    }


async def render_admin_pulse(
    lang: str = "my",
    session: AsyncSession | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Compact admin landing: read-only pulse text + admin_menu_kb.

    The full admin work surface is the Mini App Admin tab (the menu's
    Admin Suite WebApp button); chat keeps the pulse, Broadcast, and
    the payment-approval push flow (bot cleanup 2026-11-03).
    """
    if session is not None:
        data = await _fetch_pulse_metrics(session)
    else:
        async with AsyncSessionLocal() as s:
            data = await _fetch_pulse_metrics(s)

    failure = _failure_class(data["last_failed_error"])
    text = t(
        "admin_pulse_text",
        lang,
        total_users=data["total_users"],
        purchased_users=data["purchased_users"],
        jobs_today=data["jobs_today"],
        running_jobs=data["running_jobs"],
        queued_jobs=data["queued_jobs"],
        last_failure=failure if failure else t("admin_pulse_no_failure", lang),
    )
    return text, admin_menu_kb(lang=lang)


async def _render_user_welcome_fallthrough(
    message: Message,
    session: AsyncSession | None = None,
) -> None:
    """Non-admin /admin: silently fall through to the user welcome.

    Bot cleanup 2026-11-03 (agy consult): the old explicit "Admin
    only" denial confirmed the command exists and dead-ended regular
    users. A non-admin typing /admin now simply gets the normal
    /start experience — gate card when unverified, main menu when
    verified — with no denial and no mention of admin at all. Admin
    CALLBACKS keep the is_authorized_admin alert: those buttons are
    only ever rendered for admins.
    """
    from app.handlers.menu import render_main_menu
    from app.services.gate import build_gate_reminder_text, verify_user_membership
    from app.ui.keyboards import membership_gate_kb

    telegram_id = message.from_user.id

    async def _flow(s: AsyncSession) -> tuple[str, Any]:
        user, _ = await get_or_create_user(
            s,
            telegram_id=telegram_id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )
        await s.commit()
        lang = user.language_code or "my"

        if getattr(user, "status", None) == UserStatus.BLOCKED.value:
            return t("user_account_suspended", lang), None

        if settings.maintenance_mode:
            return (
                f"{t('maintenance_title', lang)}\n\n{t('maintenance_desc', lang)}",
                None,
            )

        is_verified, _ = await verify_user_membership(s, user, message.bot)
        if not is_verified:
            await s.commit()
            text = build_gate_reminder_text(lang, user.channel_verified, user.group_verified)
            kb = membership_gate_kb(
                channel_link=settings.channel_invite_link,
                group_link=settings.group_invite_link,
                lang=lang,
                channel_ok=user.channel_verified,
                group_ok=user.group_verified,
            )
            return text, kb

        menu_text, kb = await render_main_menu(telegram_id, lang, session=s)
        await s.commit()
        return f"{t('welcome_title', lang)}\n\n{t('welcome_desc', lang)}\n\n{menu_text}", kb

    if session is not None:
        text, kb = await _flow(session)
    else:
        async with AsyncSessionLocal() as s:
            text, kb = await _flow(s)

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@admin_router.message(Command("admin"))
async def handle_admin_command(message: Message, session: AsyncSession | None = None):
    if not settings.is_admin(message.from_user.id if message.from_user else None):
        await _render_user_welcome_fallthrough(message, session=session)
        return

    telegram_id = message.from_user.id
    if session is not None:
        user, _ = await get_or_create_user(session, telegram_id)
        lang = user.language_code
        text, kb = await render_admin_pulse(lang=lang, session=session)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, telegram_id)
            lang = user.language_code
            text, kb = await render_admin_pulse(lang=lang, session=s)

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@admin_router.callback_query(F.data.in_({"admin:dashboard", "admin:refresh"}))
async def handle_admin_dashboard(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    telegram_id = callback.from_user.id
    if session is not None:
        user, _ = await get_or_create_user(session, telegram_id)
        lang = user.language_code
        text, kb = await render_admin_pulse(lang=lang, session=session)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, telegram_id)
            lang = user.language_code
            text, kb = await render_admin_pulse(lang=lang, session=s)

    # Batch R (2026-11-04): was a raw edit_text with a swallowing
    # try/except — on a MEDIA message (the payment result card lives
    # on the slip photo) edit_text raises "there is no text in the
    # message to edit" and the button silently did nothing. Route
    # through the media-aware helper like the neighbouring handlers.
    await safe_edit_message(callback, text, reply_markup=kb, parse_mode="HTML")

    if callback.data == "admin:refresh":
        await callback.answer(t("admin_alert_refreshed", lang))
    else:
        await callback.answer()


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
        # Bot cleanup 2026-11-03: the old "View Pricing" button pointed
        # at the retired chat pricing screen — dropped; pricing lives
        # in the Mini App Admin tab now.
        buttons = [
            [InlineKeyboardButton(text=t("btn_back", lang), callback_data="admin:dashboard")],
        ]
        # Batch R (2026-11-04): was a raw edit_text — after approving
        # the LAST pending slip from the photo result card, this branch
        # raised "there is no text in the message to edit" and the
        # admin got the generic error popup. Media-aware helper now.
        await safe_edit_message(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
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
        recap_credits=package_recap_credits(payment.package_code),
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

    # R40 item 7: the receipt image is part of the order detail — send
    # it right here with the detail, not only behind the separate
    # "view receipt" tap or in the original notification message.
    if payment.receipt_file_id:
        try:
            await callback.message.answer_photo(
                photo=payment.receipt_file_id,
                caption=f"🧾 Payment #{payment.id} receipt (User {payment.user_id})",
            )
        except Exception as e:
            logger.warning("Could not attach receipt photo to payment detail: %s", e)


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


# ---------------------------------------------------------------------------
# Payment review: multi-admin copy sync + edit-and-approve (batch I)
# ---------------------------------------------------------------------------

_EDIT_CLAIMED_ALERT = "✏️ အခြား Admin တစ်ယောက်က ဒီ တောင်းဆိုချက်ကို ကိုင်တွယ်နေပါပြီ။"
_EDIT_ALREADY_HANDLED_ALERT = "ဒီ တောင်းဆိုချက်ကို ကိုင်တွယ်ပြီးသား ဖြစ်ပါတယ်။"


class PaymentReviewFSM(StatesGroup):
    waiting_for_corrected_minutes = State()
    # Batch AJ M4: recap-bearing packages get a second prompt for the
    # corrected recap credits (the minutes-only flow never shows it).
    waiting_for_corrected_recap_credits = State()
    confirming_edit = State()


def _claimed_by_other(payment: PaymentRequest, admin_id: int) -> bool:
    """True while a DIFFERENT admin holds the edit-and-approve claim.

    Batch N: a claim older than PAYMENT_REVIEW_CLAIM_TTL (60 min) is
    stale and no longer blocks the other admins; live claims do."""
    return (
        payment.review_claimed_by is not None
        and payment.review_claimed_by != admin_id
        and not payment_claim_is_stale(payment)
    )


def _own_copy_key(callback: CallbackQuery) -> tuple[int, int] | None:
    """(chat_id, message_id) of the message this callback belongs to —
    used to skip the acting admin's own review copy during a sync,
    because the bot handlers rewrite that message themselves."""
    msg = callback.message
    chat = getattr(msg, "chat", None) if msg is not None else None
    if chat is None:
        return None
    return (chat.id, msg.message_id)


async def _sync_review_copies(
    callback: CallbackQuery,
    payment: PaymentRequest,
    *,
    line: str,
    skip_own: bool = True,
) -> None:
    """Best-effort sync of all stored Telegram review copies; a sync
    failure must never fail the admin action that triggered it."""
    try:
        await sync_admin_copies(
            callback.bot,
            payment,
            line=line,
            skip=_own_copy_key(callback) if skip_own else None,
        )
    except Exception as exc:
        logger.warning("Payment review copy sync failed for %s: %s", payment.id, exc)


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

    if _claimed_by_other(payment, callback.from_user.id):
        await callback.answer(_EDIT_CLAIMED_ALERT, show_alert=True)
        return

    confirm_text = (
        f"⚠️ <b>Confirm Payment Approval</b>\n\n"
        f"• Payment ID: <code>{payment.id}</code>\n"
        f"• Target User: <code>{payment.user_id}</code>\n"
        f"• Package: <b>{payment.package_code}</b> ({payment.minutes} minutes + {package_recap_credits(payment.package_code)} recap credits)\n"
        f"• Amount: <b>{payment.amount_mmk:,} MMK</b>\n\n"
        f"<i>Are you sure you want to approve this payment and credit {payment.minutes} minutes + {package_recap_credits(payment.package_code)} recap credits?</i>"
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
        existing = await session.get(PaymentRequest, pay_id)
        if existing is not None and _claimed_by_other(existing, callback.from_user.id):
            await callback.answer(_EDIT_CLAIMED_ALERT, show_alert=True)
            return
        ok, payment, msg = await approve_payment_request(session, pay_id, admin_id=callback.from_user.id)
        if ok and payment:
            await session.commit()
            try:
                target_user = await session.get(User, payment.user_id)
                target_lang = target_user.language_code if target_user else "my"
                await callback.bot.send_message(
                    chat_id=payment.user_id,
                    text=t("payment_approved_user_notify", target_lang, minutes=payment.minutes, recap_credits=package_recap_credits(payment.package_code)),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("Failed to notify user %s of payment approval: %s", payment.user_id, e)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, callback.from_user.id)
            lang = user.language_code
            existing = await s.get(PaymentRequest, pay_id)
            if existing is not None and _claimed_by_other(existing, callback.from_user.id):
                await callback.answer(_EDIT_CLAIMED_ALERT, show_alert=True)
                return
            ok, payment, msg = await approve_payment_request(s, pay_id, admin_id=callback.from_user.id)
            if ok and payment:
                await s.commit()
                try:
                    target_user = await s.get(User, payment.user_id)
                    target_lang = target_user.language_code if target_user else "my"
                    await callback.bot.send_message(
                        chat_id=payment.user_id,
                        text=t("payment_approved_user_notify", target_lang, minutes=payment.minutes, recap_credits=package_recap_credits(payment.package_code)),
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.warning("Failed to notify user %s of payment approval: %s", payment.user_id, e)

    if not ok or not payment:
        await callback.answer(f"Failed to approve: {msg}", show_alert=True)
        return

    # Batch I: sync every OTHER admin's Telegram copy (keyboards
    # cleared + status line); this handler rewrites the actor's own
    # message into the result card below, so that copy is skipped.
    await _sync_review_copies(
        callback, payment, line=approved_status_line(callback.from_user.full_name, payment)
    )

    approved_text = (
        f"✅ <b>Payment Approved (#{payment.id})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• User ID: <code>{payment.user_id}</code>\n"
        f"• Package: <b>{payment.package_code}</b> ({payment.minutes} mins + {package_recap_credits(payment.package_code)} recap credits)\n"
        f"• Amount: <b>{payment.amount_mmk:,} MMK</b>\n"
        f"• Status: 🟢 <b>APPROVED</b>\n"
        f"• Approved by Admin: <code>{callback.from_user.id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Minutes and recap credits credited to user account successfully!</i>"
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

    if _claimed_by_other(payment, callback.from_user.id):
        await callback.answer(_EDIT_CLAIMED_ALERT, show_alert=True)
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
        existing = await session.get(PaymentRequest, pay_id)
        if existing is not None and _claimed_by_other(existing, callback.from_user.id):
            await callback.answer(_EDIT_CLAIMED_ALERT, show_alert=True)
            return
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
            existing = await s.get(PaymentRequest, pay_id)
            if existing is not None and _claimed_by_other(existing, callback.from_user.id):
                await callback.answer(_EDIT_CLAIMED_ALERT, show_alert=True)
                return
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

    # Batch I: sync every OTHER admin's Telegram copy (see approve).
    await _sync_review_copies(
        callback, payment, line=rejected_status_line(callback.from_user.full_name)
    )

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
# Edit-and-approve (batch I): [✏️ ပြင်ပြီးအတည်ပြုမယ်]
#
# Tapping Edit claims the request for that admin (atomic conditional
# UPDATE in payment_review.claim_payment_review): every OTHER admin's
# Telegram copy loses its keyboard and shows who is editing, and their
# approve/reject/edit taps answer "already being handled". The claimer
# gets an FSM prompt in DM, enters the corrected credit minutes, sees
# original vs corrected, and confirms or cancels. Confirm applies the
# correction and approves through the shared service; cancel releases
# the claim and restores the other copies' keyboards.
# ---------------------------------------------------------------------------


def _pay_edit_cancel_kb(pay_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚫 ပယ်ဖျက်မယ်", callback_data=f"admin:pay_edit_cancel:{pay_id}")],
        ]
    )


@admin_router.callback_query(F.data.startswith("admin:pay_edit:"))
async def handle_admin_pay_edit_start(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession | None = None
):
    if not await is_authorized_admin(callback):
        return

    pay_id = callback.data.split(":")[2]
    if session is not None:
        ok, payment, code = await claim_payment_review(session, pay_id, callback.from_user.id)
        if ok:
            await session.commit()
    else:
        async with AsyncSessionLocal() as s:
            ok, payment, code = await claim_payment_review(s, pay_id, callback.from_user.id)
            if ok:
                await s.commit()

    if not ok or payment is None:
        if code == "PAYMENT_NOT_FOUND":
            await callback.answer(t("admin_alert_payment_not_found", "my"), show_alert=True)
        elif code == "CLAIMED_BY_OTHER":
            await callback.answer(_EDIT_CLAIMED_ALERT, show_alert=True)
        else:
            await callback.answer(_EDIT_ALREADY_HANDLED_ALERT, show_alert=True)
        return

    # Claim held: other admins' copies lose their keyboards and show
    # who is editing (best-effort — a failed edit never blocks us).
    try:
        await mark_copies_claimed(
            callback.bot,
            payment,
            claimer_chat_id=callback.from_user.id,
            claimer_name=callback.from_user.full_name,
        )
    except Exception as exc:
        logger.warning("Failed to mark copies claimed for payment %s: %s", payment.id, exc)

    await state.set_state(PaymentReviewFSM.waiting_for_corrected_minutes)
    await state.update_data(payment_id=payment.id, original_minutes=payment.minutes)

    prompt_text = (
        "✏️ <b>ပြင်ပြီးအတည်ပြုမယ်</b>\n\n"
        f"• Payment ID: <code>{payment.id}</code>\n"
        f"• User: <code>{payment.user_id}</code>\n"
        f"• Package: <b>{html.escape(str(payment.package_code))}</b>\n"
        f"• လက်ရှိ အတည်ပြုမည့် မိနစ်: <b>{payment.minutes} မိနစ်</b>\n\n"
        "ပြင်ဆင်မည့် မိနစ် ပမာဏကို ဂဏန်းဖြင့် ရိုက်ထည့်ပေးပါ။"
    )
    try:
        await callback.message.answer(prompt_text, reply_markup=_pay_edit_cancel_kb(payment.id), parse_mode="HTML")
    except Exception:
        try:
            await callback.bot.send_message(
                chat_id=callback.from_user.id,
                text=prompt_text,
                reply_markup=_pay_edit_cancel_kb(payment.id),
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("Failed to send edit prompt for payment %s: %s", payment.id, exc)
    try:
        await callback.answer()
    except Exception:
        pass


@admin_router.message(PaymentReviewFSM.waiting_for_corrected_minutes)
async def handle_admin_pay_edit_minutes_input(
    message: Message, state: FSMContext, session: AsyncSession | None = None
):
    if not settings.is_admin(message.from_user.id if message.from_user else None):
        return

    data = await state.get_data()
    pay_id = data.get("payment_id")
    if not pay_id:
        await state.clear()
        return

    raw = (message.text or "").strip().replace(",", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = None
    if value is None or not (1 <= value <= 100_000):
        await message.answer(
            "⚠️ မိနစ် ပမာဏကို ဂဏန်း မှန်ကန်စွာ ရိုက်ထည့်ပေးပါ (1 – 100,000)။",
            reply_markup=_pay_edit_cancel_kb(pay_id),
            parse_mode="HTML",
        )
        return

    # Re-verify the claim before showing the confirmation: the request
    # may have been decided meanwhile (e.g. via the Mini App, which
    # also releases the claim) or the claim may no longer be ours.
    if session is not None:
        payment = await session.get(PaymentRequest, pay_id)
    else:
        async with AsyncSessionLocal() as s:
            payment = await s.get(PaymentRequest, pay_id)

    if (
        payment is None
        or payment.status != PaymentStatus.PENDING.value
        or payment.review_claimed_by != message.from_user.id
    ):
        await state.clear()
        await message.answer("ဒီ တောင်းဆိုချက်ကို ကိုင်တွယ်ပြီးသား (သို့) အခြေအနေ ပြောင်းလဲသွားပါပြီ။")
        return

    await state.update_data(corrected_minutes=value)

    # Batch AJ M4: for recap-bearing packages, the correction is only
    # half-specified with minutes — prompt for the corrected recap
    # credits too (they ride to apply_edited_approval and are granted
    # instead of the catalogue amount). Minutes-only plans keep the
    # original one-step flow.
    _pkg_recap_credits = package_recap_credits(payment.package_code)
    if _pkg_recap_credits > 0:
        await state.set_state(PaymentReviewFSM.waiting_for_corrected_recap_credits)
        await message.answer(
            "✏️ <b>Recap Credit ပြင်ဆင်ရန်</b>\n\n"
            f"• Package မူလ Recap Credit: <b>{_pkg_recap_credits}</b>\n\n"
            "ပြင်ဆင်မည့် Recap Credit ပမာဏကို ဂဏန်းဖြင့် ရိုက်ထည့်ပေးပါ (0 – 1000)။",
            reply_markup=_pay_edit_cancel_kb(pay_id),
            parse_mode="HTML",
        )
        return

    await state.set_state(PaymentReviewFSM.confirming_edit)

    confirm_text = (
        "✏️ <b>ပြင်ဆင်မှု အတည်ပြုရန်</b>\n\n"
        f"• Payment ID: <code>{payment.id}</code>\n"
        f"• မူလ မိနစ်: <b>{payment.minutes} မိနစ်</b>\n"
        f"• ပြင်ဆင်မည့် မိနစ်: <b>{value} မိနစ်</b>\n\n"
        "<i>အတည်ပြုလိုက်ပါက ပြင်ဆင်ထားသော မိနစ်ဖြင့် Credit ထည့်ပေးပါမည်။</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ အတည်ပြုမယ်", callback_data=f"admin:pay_edit_confirm:{payment.id}")],
            [InlineKeyboardButton(text="🚫 ပယ်ဖျက်မယ်", callback_data=f"admin:pay_edit_cancel:{payment.id}")],
        ]
    )
    await message.answer(confirm_text, reply_markup=kb, parse_mode="HTML")


@admin_router.message(PaymentReviewFSM.waiting_for_corrected_recap_credits)
async def handle_admin_pay_edit_recap_credits_input(
    message: Message, state: FSMContext, session: AsyncSession | None = None
):
    """Batch AJ M4: second step of the edited approval for
    recap-bearing packages — the corrected recap-credit count."""
    if not settings.is_admin(message.from_user.id if message.from_user else None):
        return

    data = await state.get_data()
    pay_id = data.get("payment_id")
    corrected_minutes = data.get("corrected_minutes")
    if not pay_id or corrected_minutes is None:
        await state.clear()
        return

    raw = (message.text or "").strip().replace(",", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = None
    if value is None or not (0 <= value <= 1000):
        await message.answer(
            "⚠️ Recap Credit ပမာဏကို ဂဏန်း မှန်ကန်စွာ ရိုက်ထည့်ပေးပါ (0 – 1000)။",
            reply_markup=_pay_edit_cancel_kb(pay_id),
            parse_mode="HTML",
        )
        return

    if session is not None:
        payment = await session.get(PaymentRequest, pay_id)
    else:
        async with AsyncSessionLocal() as s:
            payment = await s.get(PaymentRequest, pay_id)

    if (
        payment is None
        or payment.status != PaymentStatus.PENDING.value
        or payment.review_claimed_by != message.from_user.id
    ):
        await state.clear()
        await message.answer("ဒီ တောင်းဆိုချက်ကို ကိုင်တွယ်ပြီးသား (သို့) အခြေအနေ ပြောင်းလဲသွားပါပြီ။")
        return

    await state.update_data(corrected_recap_credits=value)
    await state.set_state(PaymentReviewFSM.confirming_edit)

    confirm_text = (
        "✏️ <b>ပြင်ဆင်မှု အတည်ပြုရန်</b>\n\n"
        f"• Payment ID: <code>{payment.id}</code>\n"
        f"• မူလ မိနစ်: <b>{payment.minutes} မိနစ်</b>\n"
        f"• ပြင်ဆင်မည့် မိနစ်: <b>{corrected_minutes} မိနစ်</b>\n"
        f"• မူလ Recap Credit: <b>{package_recap_credits(payment.package_code)}</b>\n"
        f"• ပြင်ဆင်မည့် Recap Credit: <b>{value}</b>\n\n"
        "<i>အတည်ပြုလိုက်ပါက ပြင်ဆင်ထားသော မိနစ် + Recap Credit ဖြင့် ထည့်ပေးပါမည်။</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ အတည်ပြုမယ်", callback_data=f"admin:pay_edit_confirm:{payment.id}")],
            [InlineKeyboardButton(text="🚫 ပယ်ဖျက်မယ်", callback_data=f"admin:pay_edit_cancel:{payment.id}")],
        ]
    )
    await message.answer(confirm_text, reply_markup=kb, parse_mode="HTML")


@admin_router.callback_query(F.data.startswith("admin:pay_edit_confirm:"))
async def handle_admin_pay_edit_confirm(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession | None = None
):
    if not await is_authorized_admin(callback):
        return

    pay_id = callback.data.split(":")[2]
    data = await state.get_data()
    corrected = data.get("corrected_minutes")
    corrected_credits = data.get("corrected_recap_credits")
    if data.get("payment_id") != pay_id or corrected is None:
        await callback.answer(
            "⏰ ဒီ ပြင်ဆင်မှုကို ဆက်လက် မလုပ်နိုင်တော့ပါ။ Edit ခလုတ်ကို ပြန်နှိပ်ပါ။",
            show_alert=True,
        )
        return

    # The service re-verifies PENDING + our claim under the approval
    # row lock, applies the correction, and approves via the shared
    # approve_payment_request() — crediting stays on the one path.
    target_lang = "my"
    if session is not None:
        ok, payment, msg = await apply_edited_approval(
            session, pay_id, callback.from_user.id, int(corrected),
            corrected_recap_credits=corrected_credits,
        )
        if ok and payment:
            await session.commit()
            target_user = await session.get(User, payment.user_id)
            target_lang = target_user.language_code if target_user else "my"
    else:
        async with AsyncSessionLocal() as s:
            ok, payment, msg = await apply_edited_approval(
                s, pay_id, callback.from_user.id, int(corrected),
                corrected_recap_credits=corrected_credits,
            )
            if ok and payment:
                await s.commit()
                target_user = await s.get(User, payment.user_id)
                target_lang = target_user.language_code if target_user else "my"

    if not ok or payment is None:
        await state.clear()
        await callback.answer(f"မအောင်မြင်ပါ: {msg}", show_alert=True)
        return

    await state.clear()

    try:
        await callback.bot.send_message(
            chat_id=payment.user_id,
            text=t(
                "payment_approved_user_notify",
                target_lang,
                minutes=payment.minutes,
                recap_credits=(
                    payment.edited_recap_credits
                    if getattr(payment, "edited_recap_credits", None) is not None
                    else package_recap_credits(payment.package_code)
                ),
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Failed to notify user %s of edited payment approval: %s", payment.user_id, e)

    # Sync ALL copies, including the claimer's own original review
    # copy (it still shows the claim state; the result card below is a
    # different message — the FSM confirmation).
    await _sync_review_copies(
        callback,
        payment,
        line=approved_status_line(callback.from_user.full_name, payment),
        skip_own=False,
    )

    done_text = (
        f"✅ <b>ပြင်ဆင်၍ အတည်ပြုပြီးပါပြီ (#{payment.id})</b>\n"
        f"• မူလ မိနစ်: <b>{payment.original_minutes} မိနစ်</b> → ပြင်ဆင်: <b>{payment.minutes} မိနစ်</b>\n"
        f"• User <code>{payment.user_id}</code> ထံ Credit ထည့်ပေးပြီးပါပြီ။"
    )
    await safe_edit_message(callback, done_text, reply_markup=None, parse_mode="HTML")
    try:
        await callback.answer("အတည်ပြုပြီးပါပြီ")
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("admin:pay_edit_cancel:"))
async def handle_admin_pay_edit_cancel(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession | None = None
):
    if not await is_authorized_admin(callback):
        return

    pay_id = callback.data.split(":")[2]
    if session is not None:
        payment = await session.get(PaymentRequest, pay_id)
        released = False
        if payment is not None and payment.review_claimed_by == callback.from_user.id:
            released = await release_payment_claim(session, pay_id, callback.from_user.id)
            if released:
                await session.commit()
    else:
        async with AsyncSessionLocal() as s:
            payment = await s.get(PaymentRequest, pay_id)
            released = False
            if payment is not None and payment.review_claimed_by == callback.from_user.id:
                released = await release_payment_claim(s, pay_id, callback.from_user.id)
                if released:
                    await s.commit()

    if payment is None:
        await callback.answer(t("admin_alert_payment_not_found", "my"), show_alert=True)
        return
    if payment.status != PaymentStatus.PENDING.value:
        await callback.answer(_EDIT_ALREADY_HANDLED_ALERT, show_alert=True)
        return
    if _claimed_by_other(payment, callback.from_user.id):
        await callback.answer(_EDIT_CLAIMED_ALERT, show_alert=True)
        return

    if released:
        # Give the other admins their review keyboards back.
        try:
            await restore_admin_copies(callback.bot, payment, skip_chat_id=callback.from_user.id)
        except Exception as exc:
            logger.warning("Failed to restore copies for payment %s: %s", payment.id, exc)

    data = await state.get_data()
    if data.get("payment_id") == pay_id:
        await state.clear()

    await safe_edit_message(
        callback,
        "🚫 <b>ပြင်ဆင်မှု ပယ်ဖျက်ပြီးပါပြီ</b>\nတောင်းဆိုချက်ကို မူလအတိုင်း ဆက်လက် စစ်ဆေးနိုင်ပါပြီ။",
        reply_markup=None,
        parse_mode="HTML",
    )
    try:
        await callback.answer("ပယ်ဖျက်ပြီးပါပြီ")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Guide Video Management Handlers
# ---------------------------------------------------------------------------
# Dynamic Tutorial Video Management
# ---------------------------------------------------------------------------

async def _get_tutorial_state(session: AsyncSession) -> dict[str, Any]:
    link_row = (await session.execute(select(BotSetting).where(BotSetting.key == "tutorial_video_link"))).scalar_one_or_none()
    current_link = link_row.value if link_row is not None else settings.tutorial_video_link

    en_row = (await session.execute(select(BotSetting).where(BotSetting.key == "tutorial_video_enabled"))).scalar_one_or_none()
    is_enabled = (en_row.value.lower() == "true") if en_row else True

    v_en = (await session.execute(select(BotSetting).where(BotSetting.key == "tutorial_voice_enabled"))).scalar_one_or_none()
    voice_enabled = (v_en.value.lower() == "true") if v_en else True

    v_link = (await session.execute(select(BotSetting).where(BotSetting.key == "tutorial_voice_link"))).scalar_one_or_none()
    voice_link = v_link.value if v_link else current_link

    r_en = (await session.execute(select(BotSetting).where(BotSetting.key == "tutorial_recap_enabled"))).scalar_one_or_none()
    recap_enabled = (r_en.value.lower() == "true") if r_en else True

    r_link = (await session.execute(select(BotSetting).where(BotSetting.key == "tutorial_recap_link"))).scalar_one_or_none()
    recap_link = r_link.value if r_link else current_link

    return {
        "video_link": current_link,
        "is_enabled": is_enabled,
        "voice_enabled": voice_enabled,
        "voice_link": voice_link,
        "recap_enabled": recap_enabled,
        "recap_link": recap_link,
    }


@admin_router.callback_query(F.data == "admin:tutorial")
async def handle_admin_tutorial(callback: CallbackQuery, session: AsyncSession | None = None):
    if not await is_authorized_admin(callback):
        return

    if session is not None:
        state_data = await _get_tutorial_state(session)
    else:
        async with AsyncSessionLocal() as s:
            state_data = await _get_tutorial_state(s)

    v_badge = "🟢 <b>Enabled (ဖွင့်ထားသည်)</b>" if state_data["voice_enabled"] else "🔴 <b>Disabled (ပိတ်ထားသည်)</b>"
    r_badge = "🟢 <b>Enabled (ဖွင့်ထားသည်)</b>" if state_data["recap_enabled"] else "🔴 <b>Disabled (ပိတ်ထားသည်)</b>"

    # R40 item 15e: long guide URLs inside <code> never wrapped and
    # ran off the message edge — wrap them.
    v_link_disp = f"<code>{_wrap_admin_value(state_data['voice_link'], width=44)}</code>" if state_data['voice_link'] else "<i>Not set</i>"
    r_link_disp = f"<code>{_wrap_admin_value(state_data['recap_link'], width=44)}</code>" if state_data['recap_link'] else "<i>Not set</i>"

    text = (
        f"📺 <b>Tutorial Video Manager (Guide Controls)</b>\n\n"
        f"🎙️ <b>Voice Clone Tutorial:</b>\n"
        f"• Status: {v_badge}\n"
        f"• Link: {v_link_disp}\n\n"
        f"🎬 <b>Movie Recap Tutorial:</b>\n"
        f"• Status: {r_badge}\n"
        f"• Link: {r_link_disp}\n\n"
        f"ℹ️ <i>ဗီဒီယို အသင့်မဖြစ်သေးပါက ခလုတ်ကို နှိပ်၍ ပိတ်ထားနိုင်ပြီး၊ အဆင်သင့်ဖြစ်ပါက ဖွင့်၍ Link အသစ် ပြောင်းလဲနိုင်ပါသည်။</i>"
    )
    kb = admin_tutorial_mgmt_kb(
        voice_enabled=state_data["voice_enabled"],
        voice_link=state_data["voice_link"],
        recap_enabled=state_data["recap_enabled"],
        recap_link=state_data["recap_link"],
        lang="en",
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data == "admin:tutorial:voice_toggle")
async def handle_admin_tutorial_voice_toggle(callback: CallbackQuery):
    if not await is_authorized_admin(callback):
        return

    async with AsyncSessionLocal() as session:
        stmt = select(BotSetting).where(BotSetting.key == "tutorial_voice_enabled")
        row = (await session.execute(stmt)).scalar_one_or_none()
        current_val = (row.value.lower() == "true") if row else True
        new_val = not current_val

        # Through the one settings write path (hunt C2): persists,
        # audits (settings.update) and applies to memory together —
        # the old direct BotSetting write left the in-memory value
        # stale until the next reload.
        await set_dynamic_setting(session, "tutorial_voice_enabled", new_val, admin_id=callback.from_user.id)
        await session.commit()

    status_str = "Enabled" if new_val else "Disabled"
    await callback.answer(f"Voice Clone Tutorial {status_str}!", show_alert=False)
    await handle_admin_tutorial(callback)


@admin_router.callback_query(F.data == "admin:tutorial:recap_toggle")
async def handle_admin_tutorial_recap_toggle(callback: CallbackQuery):
    if not await is_authorized_admin(callback):
        return

    async with AsyncSessionLocal() as session:
        stmt = select(BotSetting).where(BotSetting.key == "tutorial_recap_enabled")
        row = (await session.execute(stmt)).scalar_one_or_none()
        current_val = (row.value.lower() == "true") if row else True
        new_val = not current_val

        # Through the one settings write path (hunt C2), as above.
        await set_dynamic_setting(session, "tutorial_recap_enabled", new_val, admin_id=callback.from_user.id)
        await session.commit()

    status_str = "Enabled" if new_val else "Disabled"
    await callback.answer(f"Movie Recap Tutorial {status_str}!", show_alert=False)
    await handle_admin_tutorial(callback)


@admin_router.callback_query(F.data.in_({"admin:tutorial:voice_edit", "admin:tutorial_edit_url"}))
async def handle_admin_tutorial_edit_voice_url(callback: CallbackQuery, state: FSMContext):
    if not await is_authorized_admin(callback):
        return

    await state.set_state(AdminTutorialFSM.waiting_for_voice_url)
    text = (
        "✏️ <b>Enter Voice Clone Guide Video URL</b>\n\n"
        "Please type or paste the video link for Voice Clone Tutorial:\n"
        "Example: <code>https://t.me/VoiceClonePro/29</code>"
    )
    kb = admin_tutorial_cancel_kb("en")
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data == "admin:tutorial:recap_edit")
async def handle_admin_tutorial_edit_recap_url(callback: CallbackQuery, state: FSMContext):
    if not await is_authorized_admin(callback):
        return

    await state.set_state(AdminTutorialFSM.waiting_for_recap_url)
    text = (
        "✏️ <b>Enter Movie Recap Guide Video URL</b>\n\n"
        "Please type or paste the video link for Movie Recap Tutorial:\n"
        "Example: <code>https://t.me/VoiceClonePro/30</code>"
    )
    kb = admin_tutorial_cancel_kb("en")
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.message(AdminTutorialFSM.waiting_for_voice_url)
async def handle_admin_tutorial_voice_url_input(message: Message, state: FSMContext):
    if not settings.is_admin(message.from_user.id if message.from_user else None):
        return

    url = (message.text or "").strip()
    # Same t.me validation as the Mini App settings save (hunt #5
    # parity): this receiver used to accept any http(s) string, so the
    # bot path could store exactly the malformed link the webapp path
    # was fixed to reject -- and a bad stored link breaks the main
    # menu's URL button for every user.
    if not is_valid_telegram_link(url):
        await message.answer(
            "⚠️ <b>Invalid URL format!</b>\n"
            "The link must be a Telegram link, e.g. <code>https://t.me/VoiceClonePro/29</code>.\n"
            "Please try again or tap cancel below:",
            reply_markup=admin_tutorial_cancel_kb("en"),
            parse_mode="HTML",
        )
        return

    await state.clear()
    async with AsyncSessionLocal() as session:
        admin_user, _ = await get_or_create_user(session, message.from_user.id)
        lang = admin_user.language_code if admin_user else "my"
        # Through the one settings write path (hunt C2).
        await set_dynamic_setting(session, "tutorial_voice_link", url, admin_id=message.from_user.id)
        await session.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📺 Back to Guide Video Mgr", callback_data="admin:tutorial")],
        [InlineKeyboardButton(text=t("btn_admin_dashboard", lang), callback_data="admin:dashboard")],
    ])
    await message.answer(
        f"✅ <b>Voice Clone Guide Video URL Updated!</b>\n\n• New URL: <code>{html.escape(url)}</code>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@admin_router.message(AdminTutorialFSM.waiting_for_recap_url)
async def handle_admin_tutorial_recap_url_input(message: Message, state: FSMContext):
    if not settings.is_admin(message.from_user.id if message.from_user else None):
        return

    url = (message.text or "").strip()
    # Same t.me validation as the Mini App settings save (hunt #5
    # parity) -- see the voice receiver above.
    if not is_valid_telegram_link(url):
        await message.answer(
            "⚠️ <b>Invalid URL format!</b>\n"
            "The link must be a Telegram link, e.g. <code>https://t.me/VoiceClonePro/30</code>.\n"
            "Please try again or tap cancel below:",
            reply_markup=admin_tutorial_cancel_kb("en"),
            parse_mode="HTML",
        )
        return

    await state.clear()
    async with AsyncSessionLocal() as session:
        admin_user, _ = await get_or_create_user(session, message.from_user.id)
        lang = admin_user.language_code if admin_user else "my"
        # Through the one settings write path (hunt C2).
        await set_dynamic_setting(session, "tutorial_recap_link", url, admin_id=message.from_user.id)
        await session.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📺 Back to Guide Video Mgr", callback_data="admin:tutorial")],
        [InlineKeyboardButton(text=t("btn_admin_dashboard", lang), callback_data="admin:dashboard")],
    ])
    await message.answer(
        f"✅ <b>Movie Recap Guide Video URL Updated!</b>\n\n• New URL: <code>{html.escape(url)}</code>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@admin_router.callback_query(F.data == "admin:tutorial_toggle")
async def handle_admin_tutorial_toggle(callback: CallbackQuery):
    await handle_admin_tutorial_voice_toggle(callback)


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
        # Through the one settings write path (hunt C2).
        await set_dynamic_setting(session, "tutorial_video_link", "", admin_id=callback.from_user.id)
        await session.commit()

    await callback.answer("Guide video link removed!", show_alert=False)
    await handle_admin_tutorial(callback)


# Backward-compatible alias for unit tests & legacy callers
handle_admin_tutorial_url_input = handle_admin_tutorial_voice_url_input


# ---------------------------------------------------------------------------
# Broadcast Handlers
# ---------------------------------------------------------------------------

def _broadcast_group_label(lang: str, group_str: str) -> str:
    """Localized display label for a broadcast audience segment."""
    key = {
        BroadcastRecipientGroup.REGISTERED_ALL.value: "admin_bc_group_registered",
        BroadcastRecipientGroup.IMPORTED_ALL.value: "admin_bc_group_imported_all",
        BroadcastRecipientGroup.IMPORTED_UNREGISTERED.value: "admin_bc_group_imported_unregistered",
    }.get(str(group_str), "admin_bc_group_registered")
    return t(key, lang)


def _broadcast_audience_text(lang: str, reg_count: int, imp_all_count: int, imp_unreg_count: int) -> str:
    """Audience header with the segment counts AND the one-line
    reconciliation (bot cleanup 2026-11-03): the three segments are
    not disjoint — Imported Unregistered is a subset of Imported All,
    and Imported All can include people who are also Registered. The
    counts themselves come straight from get_broadcast_recipient_ids,
    so the header can never promise a reach the sender will not get.
    """
    return t(
        "admin_broadcast_audience_text",
        lang,
        reg_count=reg_count,
        imp_all_count=imp_all_count,
        imp_unreg_count=imp_unreg_count,
    )


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

    text = _broadcast_audience_text(lang, reg_count, imp_all_count, imp_unreg_count)
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

    text = _broadcast_audience_text(lang, reg_count, imp_all_count, imp_unreg_count)
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

    text = t(
        "admin_broadcast_compose_text",
        lang,
        group=_broadcast_group_label(lang, group_str),
        count=recipient_count,
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
    if broadcast_text.startswith("/"):
        await state.clear()
        return

    if not broadcast_text or len(broadcast_text) < 4:
        await message.answer("⚠️ Broadcast စာသား တိုလွန်းပါသည်။ မတော်တဆ မှားယွင်းပေးပို့ခြင်း မဖြစ်စေရန် အနည်းဆုံး အက္ခရာ ၄ လုံး ရိုက်ထည့်ပါရန်။")
        return

    if len(broadcast_text) > 4096:
        # Telegram rejects message text over 4096 chars; without this
        # check the campaign was created and every recipient silently
        # skipped (hunt #3 B2). Reject at compose time, before any
        # campaign exists; the admin stays in waiting_for_message.
        await message.answer("⚠️ Broadcast စာသား ရှည်လွန်းပါသည်။ Telegram ကန့်သတ်ချက် (အက္ခရာ ၄၀၉၆ လုံး) ထက် ကျော်နေပါသည် — တိုအောင် ပြင်ပြီး ပြန်ပို့ပါရန်။")
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
    preview_text = t(
        "admin_broadcast_preview_text",
        lang,
        group=_broadcast_group_label(lang, target_group),
        count=recipient_count,
        preview=safe_preview,
    )
    await message.answer(
        preview_text,
        reply_markup=admin_broadcast_preview_kb(token, lang),
        parse_mode="HTML",
    )


@admin_router.callback_query(F.data.startswith("admin:broadcast_test:"))
async def handle_admin_broadcast_test(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession | None = None,
):
    """Test-send the composed broadcast to the admin's own chat only.

    Bot cleanup 2026-11-03: fits the existing confirming state — the
    nonce is validated exactly like the real send, the FSM state is
    left untouched (the admin can still confirm or cancel), and the
    ONLY recipient is the admin themselves. No campaign is created.
    """
    if not await is_authorized_admin(callback):
        return

    token = callback.data.split("admin:broadcast_test:")[1]
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

    # Sent exactly the way deliver_broadcast sends (plain text, no
    # parse mode) so the admin sees precisely what recipients get.
    await callback.bot.send_message(
        chat_id=callback.from_user.id,
        text=broadcast_text,
    )
    await callback.answer(t("admin_alert_broadcast_test_sent", lang))


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

    if session is not None:
        text, kb = await render_admin_pulse(lang=lang, session=session)
    else:
        async with AsyncSessionLocal() as s:
            text, kb = await render_admin_pulse(lang=lang, session=s)

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


