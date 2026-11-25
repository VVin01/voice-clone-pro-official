from __future__ import annotations

import html
import json
import logging
import re
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.models.enums import PaymentStatus
from app.models.schema import AuditLog, PaymentRequest, User
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
    admin_pay_approve_confirm_kb,
    admin_pay_reject_confirm_kb,
)
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
        recap_credits=(
            getattr(payment, "recap_credits", None)
            if getattr(payment, "recap_credits", None) is not None
            else package_recap_credits(payment.package_code)
        ),
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

    await safe_edit_message(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None, parse_mode="HTML")
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

    _eff_rc = _payment_effective_recap_credits(payment)
    confirm_text = (
        f"⚠️ <b>Confirm Payment Approval</b>\n\n"
        f"• Payment ID: <code>{payment.id}</code>\n"
        f"• Target User: <code>{payment.user_id}</code>\n"
        f"• Package: <b>{payment.package_code}</b> ({payment.minutes} minutes + {_eff_rc} recap credits)\n"
        f"• Amount: <b>{payment.amount_mmk:,} MMK</b>\n\n"
        f"<i>Are you sure you want to approve this payment and credit {payment.minutes} minutes + {_eff_rc} recap credits?</i>"
    )
    kb = admin_pay_approve_confirm_kb(payment.id, lang=lang)
    await safe_edit_message(callback, confirm_text, reply_markup=kb, parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


def _payment_effective_recap_credits(payment: PaymentRequest) -> int:
    edited = getattr(payment, "edited_recap_credits", None)
    if edited is not None:
        return int(edited)
    snap = getattr(payment, "recap_credits", None)
    if snap is not None and int(snap) > 0:
        return int(snap)
    return int(package_recap_credits(payment.package_code))


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
                    text=t("payment_approved_user_notify", target_lang, minutes=payment.minutes, recap_credits=_payment_effective_recap_credits(payment)),
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
                        text=t("payment_approved_user_notify", target_lang, minutes=payment.minutes, recap_credits=_payment_effective_recap_credits(payment)),
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
        f"• Package: <b>{payment.package_code}</b> ({payment.minutes} mins + {_payment_effective_recap_credits(payment)} recap credits)\n"
        f"• Amount: <b>{payment.amount_mmk:,} MMK</b>\n"
        f"• Status: 🟢 <b>APPROVED</b>\n"
        f"• Approved by Admin: <code>{callback.from_user.id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Minutes and recap credits credited to user account successfully!</i>"
    )
    await safe_edit_message(callback, approved_text, reply_markup=None, parse_mode="HTML")
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
    await safe_edit_message(callback, rejected_text, reply_markup=None, parse_mode="HTML")
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


def _pay_edit_cancel_kb(pay_id: str, *, include_promo: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if include_promo:
        rows.append(
            [
                InlineKeyboardButton(
                    text="+20% ပရိုမို",
                    callback_data=f"admin:pay_edit_promo20:{pay_id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="🚫 ပယ်ဖျက်မယ်", callback_data=f"admin:pay_edit_cancel:{pay_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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

    base_rc = _payment_effective_recap_credits(payment)
    rc_line = f"• လက်ရှိ အတည်ပြုမည့် Recap Credit: <b>{base_rc}</b>\n" if base_rc > 0 else ""
    prompt_text = (
        "✏️ <b>ပြင်ပြီးအတည်ပြုမယ်</b>\n\n"
        f"• Payment ID: <code>{payment.id}</code>\n"
        f"• User: <code>{payment.user_id}</code>\n"
        f"• Package: <b>{html.escape(str(payment.package_code))}</b>\n"
        f"• လက်ရှိ အတည်ပြုမည့် မိနစ်: <b>{payment.minutes} မိနစ်</b>\n"
        f"{rc_line}\n"
        "ပြင်ဆင်မည့် မိနစ် ပမာဏကို ဂဏန်းဖြင့် ရိုက်ထည့်ပါ (သို့မဟုတ် <b>+20% ပရိုမို</b> ခလုတ်ကို နှိပ်ပါ)။"
    )
    start_kb = _pay_edit_cancel_kb(payment.id, include_promo=True)
    try:
        await callback.message.answer(prompt_text, reply_markup=start_kb, parse_mode="HTML")
    except Exception:
        try:
            await callback.bot.send_message(
                chat_id=callback.from_user.id,
                text=prompt_text,
                reply_markup=start_kb,
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("Failed to send edit prompt for payment %s: %s", payment.id, exc)
    try:
        await callback.answer()
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("admin:pay_edit_promo20:"))
async def handle_admin_pay_edit_promo20(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession | None = None
):
    """1-tap +20% promo preset inside Bot DM edit-approve flow (minutes x1.2 rounded, credits x1.2 2dp)."""
    if not await is_authorized_admin(callback):
        return

    pay_id = callback.data.split(":")[2]
    if session is not None:
        payment = await session.get(PaymentRequest, pay_id)
    else:
        async with AsyncSessionLocal() as s:
            payment = await s.get(PaymentRequest, pay_id)

    if (
        payment is None
        or payment.status != PaymentStatus.PENDING.value
        or payment.review_claimed_by != callback.from_user.id
    ):
        await state.clear()
        await callback.answer(_EDIT_ALREADY_HANDLED_ALERT, show_alert=True)
        return

    base_mins = int(payment.minutes or 0)
    base_rc = _payment_effective_recap_credits(payment)
    promo_mins = int(round(base_mins * 1.2))
    promo_rc_f = round(float(base_rc) * 1.2, 2)
    promo_rc: int | float = int(promo_rc_f) if promo_rc_f == int(promo_rc_f) else promo_rc_f

    await state.update_data(
        payment_id=payment.id,
        original_minutes=base_mins,
        corrected_minutes=promo_mins,
        corrected_recap_credits=promo_rc if base_rc > 0 else None,
    )
    await state.set_state(PaymentReviewFSM.confirming_edit)

    rc_block = (
        f"• မူလ Recap Credit: <b>{base_rc}</b>\n"
        f"• +20% ပရိုမို Recap Credit: <b>{promo_rc}</b>\n"
        if base_rc > 0
        else ""
    )
    confirm_text = (
        "✏️ <b>+20% ပရိုမို ပြင်ဆင်မှု အတည်ပြုရန်</b>\n\n"
        f"• Payment ID: <code>{payment.id}</code>\n"
        f"• မူလ မိနစ်: <b>{base_mins} မိနစ်</b>\n"
        f"• +20% ပရိုမို မိနစ်: <b>{promo_mins} မိနစ်</b>\n"
        f"{rc_block}\n"
        "<i>အတည်ပြုလိုက်ပါက +20% ပရိုမို ပမာဏဖြင့် ချက်ချင်း ထည့်ပေးပါမည်။</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ အတည်ပြုမယ်", callback_data=f"admin:pay_edit_confirm:{payment.id}")],
            [InlineKeyboardButton(text="🚫 ပယ်ဖျက်မယ်", callback_data=f"admin:pay_edit_cancel:{payment.id}")],
        ]
    )
    await safe_edit_message(callback, confirm_text, reply_markup=kb, parse_mode="HTML")
    try:
        await callback.answer("+20% ပရိုမို ပမာဏ တွက်ချက်ပြီးပါပြီ")
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
    if value is None or not (0 <= value <= 100_000):
        await message.answer(
            "⚠️ မိနစ် ပမာဏကို ဂဏန်း မှန်ကန်စွာ ရိုက်ထည့်ပေးပါ (0 – 100,000)။",
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

    _pkg_recap_credits = _payment_effective_recap_credits(payment)
    if value <= 0 and _pkg_recap_credits <= 0:
        await message.answer(
            "⚠️ မိနစ် ပမာဏကို ဂဏန်း မှန်ကန်စွာ ရိုက်ထည့်ပေးပါ (1 – 100,000)။",
            reply_markup=_pay_edit_cancel_kb(pay_id),
            parse_mode="HTML",
        )
        return

    await state.update_data(corrected_minutes=value)

    # Batch AJ M4: for recap-bearing packages, the correction is only
    # half-specified with minutes — prompt for the corrected recap
    # credits too (they ride to apply_edited_approval and are granted
    # instead of the catalogue amount). Minutes-only plans keep the
    # original one-step flow.
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
        raw_f = round(float(raw), 2)
        value: int | float | None = int(raw_f) if raw_f == int(raw_f) else raw_f
    except (TypeError, ValueError):
        value = None
    if value is None or not (0 <= float(value) <= 1000):
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
        f"• မူလ Recap Credit: <b>{_payment_effective_recap_credits(payment)}</b>\n"
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
                recap_credits=_payment_effective_recap_credits(payment),
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
# Manual Group Moderation sub-router (runs inside admin_router, before group_admin_router)
# ---------------------------------------------------------------------------

from app.handlers.group_moderation import group_moderation_router as _group_moderation_router

if _group_moderation_router.parent_router is not admin_router:
    admin_router.include_router(_group_moderation_router)
