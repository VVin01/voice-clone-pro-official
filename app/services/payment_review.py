"""Multi-admin Telegram review sync for payment requests (batch I).

Every admin receives their own Telegram copy of a payment review
message (bot slip flow: photo/document/text with buttons; Mini App
slip flow: photo alert). The copies are recorded on the PaymentRequest
row (``admin_notify_copies`` JSON) when they are sent, and this module
keeps them in sync afterwards:

- a terminal action by ANY admin (Telegram buttons or the Mini App
  admin endpoints) clears the inline keyboard on EVERY copy and
  appends a Burmese status line naming the acting admin;
- the edit-and-approve flow claims the request for one admin: the
  other admins' copies lose their keyboards while the claim is held,
  and get the original text + fresh review keyboard back if the
  claim is cancelled.

All approve/reject crediting still goes through
``app.services.payments.approve_payment_request`` /
``reject_payment_request`` (row lock + PENDING check + idempotent
ledger effects); this module never credits anything itself. The one
exception is the claim bookkeeping and, for edit-and-approve, the
minutes correction that is applied to the row BEFORE the shared
approval service runs, so the approval credits the corrected amount
through the exact same code path as a plain approval.
"""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PaymentStatus
from app.models.schema import AuditLog, PaymentRequest, utc_now
from app.services.payments import (
    PAYMENT_REVIEW_CLAIM_TTL,
    approve_payment_request,
    payment_claim_is_stale,
)

logger = logging.getLogger(__name__)

_CAPTION_LIMIT = 1024  # Telegram media caption limit
_TEXT_LIMIT = 4096  # Telegram text message limit


# ── Claim TTL (batch N) ──────────────────────────────────────────────

# Batch AJ (M3): PAYMENT_REVIEW_CLAIM_TTL and payment_claim_is_stale
# moved to app.services.payments — the claim is now enforced inside
# approve/reject_payment_request themselves, and this module already
# imports that one (the reverse import would be circular). They are
# re-exported here so every existing import site keeps working.


# ── Stored admin copies ──────────────────────────────────────────────


def parse_admin_copies(payment: PaymentRequest) -> list[dict[str, Any]]:
    """Decode the stored Telegram review copies for a payment request.

    Each copy: {"chat_id": int, "message_id": int,
    "kind": "photo" | "document" | "text", "text": <original body>}.
    Legacy rows (pre-batch-I) have no copies and simply never sync.
    """
    raw = getattr(payment, "admin_notify_copies", None)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Payment %s has undecodable admin_notify_copies", payment.id)
        return []
    if not isinstance(data, list):
        return []
    return [c for c in data if isinstance(c, dict) and "chat_id" in c and "message_id" in c]


def add_admin_copy(
    payment: PaymentRequest,
    *,
    chat_id: int,
    message_id: int,
    kind: str,
    text: str,
) -> None:
    """Append one admin's Telegram copy to the row (caller commits)."""
    copies = parse_admin_copies(payment)
    copies.append(
        {"chat_id": int(chat_id), "message_id": int(message_id), "kind": kind, "text": text}
    )
    payment.admin_notify_copies = json.dumps(copies, ensure_ascii=False)


# ── Status lines ─────────────────────────────────────────────────────


def _actor(actor_name: str | None) -> str:
    name = html.escape(str(actor_name or "").strip())
    return name or "Admin"


def approved_status_line(actor_name: str | None, payment: PaymentRequest) -> str:
    """'✅ <name> က အတည်ပြုပြီးပါပြီ' (+ the edit note when corrected)."""
    line = f"✅ {_actor(actor_name)} က အတည်ပြုပြီးပါပြီ"
    if payment.original_minutes is not None and payment.edited_by is not None:
        line += f"\n✏️ မိနစ် ပြင်ဆင်၍ အတည်ပြုခဲ့သည်: {payment.original_minutes} → {payment.minutes} မိနစ်"
    return line


def rejected_status_line(actor_name: str | None) -> str:
    """'🚫 <name> က ငြင်းပယ်ပြီးပါပြီ'."""
    return f"🚫 {_actor(actor_name)} က ငြင်းပယ်ပြီးပါပြီ"


def claimed_status_line(actor_name: str | None) -> str:
    """Line shown on other admins' copies while an edit claim is held."""
    return f"✏️ {_actor(actor_name)} က ပြင်ဆင်နေပါသည်"


# ── Copy editing ─────────────────────────────────────────────────────


def _append_line(original: str, line: str, *, media: bool) -> str:
    text = f"{original}\n\n{line}" if original else line
    limit = _CAPTION_LIMIT if media else _TEXT_LIMIT
    if len(text) > limit:
        text = text[: limit - 4] + "..."
    return text


async def _edit_copy(bot: Any, copy: dict[str, Any], new_text: str, reply_markup: Any) -> bool:
    """Edit one stored copy; failures (too old / deleted / blocked)
    are logged and reported as False — they must never fail the
    admin action that triggered the sync."""
    kind = copy.get("kind")
    try:
        if kind in ("photo", "document"):
            await bot.edit_message_caption(
                chat_id=copy["chat_id"],
                message_id=copy["message_id"],
                caption=new_text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        else:
            await bot.edit_message_text(
                chat_id=copy["chat_id"],
                message_id=copy["message_id"],
                text=new_text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        return True
    except Exception as exc:
        logger.warning(
            "Could not sync admin copy (chat %s, message %s): %s",
            copy.get("chat_id"),
            copy.get("message_id"),
            exc,
        )
        return False


async def sync_admin_copies(
    bot: Any,
    payment: PaymentRequest,
    *,
    line: str,
    skip: tuple[int, int] | None = None,
) -> int:
    """Append ``line`` to every stored copy and remove its keyboard.

    ``skip`` is the (chat_id, message_id) of the acting admin's own
    copy when the caller already rewrote that message itself (the bot
    approve/reject handlers render their own result card). Returns the
    number of copies successfully updated.
    """
    updated = 0
    for copy in parse_admin_copies(payment):
        if skip is not None and (copy["chat_id"], copy["message_id"]) == skip:
            continue
        media = copy.get("kind") in ("photo", "document")
        new_text = _append_line(str(copy.get("text") or ""), line, media=media)
        if await _edit_copy(bot, copy, new_text, None):
            updated += 1
    return updated


async def mark_copies_claimed(
    bot: Any,
    payment: PaymentRequest,
    *,
    claimer_chat_id: int,
    claimer_name: str | None,
) -> int:
    """While an edit claim is held: clear the OTHER admins' keyboards
    and note who is editing. The claimer's own copies stay untouched
    (their FSM prompt lives in separate messages)."""
    line = claimed_status_line(claimer_name)
    updated = 0
    for copy in parse_admin_copies(payment):
        if copy["chat_id"] == claimer_chat_id:
            continue
        media = copy.get("kind") in ("photo", "document")
        new_text = _append_line(str(copy.get("text") or ""), line, media=media)
        if await _edit_copy(bot, copy, new_text, None):
            updated += 1
    return updated


async def restore_admin_copies(
    bot: Any,
    payment: PaymentRequest,
    *,
    skip_chat_id: int,
) -> int:
    """Undo mark_copies_claimed after a cancelled edit: the other
    admins' copies get the original text and a fresh review keyboard
    back, so any of them can act on the request again."""
    from app.ui.keyboards import admin_pay_review_kb

    kb = admin_pay_review_kb(payment.id)
    updated = 0
    for copy in parse_admin_copies(payment):
        if copy["chat_id"] == skip_chat_id:
            continue
        if await _edit_copy(bot, copy, str(copy.get("text") or ""), kb):
            updated += 1
    return updated


# ── Edit-and-approve claim bookkeeping ───────────────────────────────


async def claim_payment_review(
    session: AsyncSession,
    payment_id: str,
    admin_id: int,
) -> tuple[bool, PaymentRequest | None, str]:
    """Atomically claim a PENDING request for edit-and-approve.

    The conditional UPDATE is the claim: it only lands when the
    request is still PENDING and either unclaimed or already claimed
    by the same admin (a re-tap by the claimer simply re-enters the
    flow). Returns (ok, payment, code) with code one of CLAIMED /
    PAYMENT_NOT_FOUND / ALREADY_<status> / CLAIMED_BY_OTHER.
    """
    # Lazy TTL expiry (batch N): first clear THIS request's claim if
    # it is another admin's and older than PAYMENT_REVIEW_CLAIM_TTL,
    # so the claim UPDATE below can land; a live claim is untouched
    # and still resolves to CLAIMED_BY_OTHER.
    stale_cutoff = utc_now() - PAYMENT_REVIEW_CLAIM_TTL
    await session.execute(
        update(PaymentRequest)
        .where(
            PaymentRequest.id == payment_id,
            PaymentRequest.status == PaymentStatus.PENDING.value,
            PaymentRequest.review_claimed_by.is_not(None),
            PaymentRequest.review_claimed_by != admin_id,
            PaymentRequest.review_claimed_at.is_not(None),
            PaymentRequest.review_claimed_at < stale_cutoff,
        )
        .values(review_claimed_by=None, review_claimed_at=None)
        # SQL-level only: the ORM session-sync evaluator would compare
        # the cutoff against the in-memory (naive, post-SQLite-refresh)
        # claimed_at and raise on naive/aware mixing; the
        # populate_existing select below refreshes the row anyway.
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    stmt = (
        update(PaymentRequest)
        .where(
            PaymentRequest.id == payment_id,
            PaymentRequest.status == PaymentStatus.PENDING.value,
            (PaymentRequest.review_claimed_by.is_(None))
            | (PaymentRequest.review_claimed_by == admin_id),
        )
        .values(review_claimed_by=admin_id, review_claimed_at=utc_now())
    )
    res = await session.execute(stmt)
    await session.flush()
    payment = (
        await session.execute(
            select(PaymentRequest)
            .where(PaymentRequest.id == payment_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if payment is None:
        return False, None, "PAYMENT_NOT_FOUND"
    if res.rowcount:
        return True, payment, "CLAIMED"
    if payment.status != PaymentStatus.PENDING.value:
        return False, payment, f"ALREADY_{payment.status}"
    return False, payment, "CLAIMED_BY_OTHER"


async def release_payment_claim(
    session: AsyncSession,
    payment_id: str,
    admin_id: int,
) -> bool:
    """Release the caller's own claim (edit cancelled). Conditional on
    the caller actually holding it, so a stale cancel can never steal
    another admin's claim."""
    stmt = (
        update(PaymentRequest)
        .where(
            PaymentRequest.id == payment_id,
            PaymentRequest.review_claimed_by == admin_id,
        )
        .values(review_claimed_by=None, review_claimed_at=None)
    )
    res = await session.execute(stmt)
    await session.flush()
    return bool(res.rowcount)


async def apply_edited_approval(
    session: AsyncSession,
    payment_id: str,
    admin_id: int,
    corrected_minutes: int,
    corrected_recap_credits: int | None = None,
) -> tuple[bool, PaymentRequest | None, str]:
    """Approve with a corrected minute amount, via the shared service.

    Re-verifies (under the same row lock the approval uses) that the
    request is still PENDING and still claimed by this admin, records
    the audit fields (original_minutes — the FIRST pre-edit value —
    and edited_by) plus an AuditLog entry, applies the correction to
    ``payment.minutes``, and then runs the standard
    approve_payment_request() so crediting, recap credits and the
    approval audit log are byte-identical to a plain approval.

    Batch AJ (M4): the bounds live HERE, in the service — previously
    the 100,000-minute cap existed only in the bot FSM, so any other
    caller of this function could credit unbounded minutes. The
    optional recap-credits correction (0 – 1,000) is recorded on the
    row (edited_recap_credits) and honoured by the approval's recap
    grant instead of the catalogue amount.
    """
    if int(corrected_minutes) <= 0 or int(corrected_minutes) > 100_000:
        return False, None, "INVALID_MINUTES"
    if corrected_recap_credits is not None and not (
        0 <= int(corrected_recap_credits) <= 1000
    ):
        return False, None, "INVALID_RECAP_CREDITS"
    stmt = select(PaymentRequest).where(PaymentRequest.id == payment_id).with_for_update()
    payment = (await session.execute(stmt)).scalar_one_or_none()
    if payment is None:
        return False, None, "PAYMENT_NOT_FOUND"
    if payment.status != PaymentStatus.PENDING.value:
        return False, payment, f"ALREADY_{payment.status}"
    if payment.review_claimed_by != admin_id:
        return False, payment, "NOT_CLAIMED_BY_ADMIN"

    if payment.original_minutes is None:
        payment.original_minutes = payment.minutes
    original = payment.original_minutes
    payment.minutes = int(corrected_minutes)
    payment.edited_by = admin_id
    if corrected_recap_credits is not None:
        payment.edited_recap_credits = int(corrected_recap_credits)
    session.add(
        AuditLog(
            admin_id=admin_id,
            action="payment.edit_approve",
            target_type="payment_request",
            target_id=str(payment.id),
            details_json=json.dumps(
                {
                    "user_id": payment.user_id,
                    "original_minutes": original,
                    "corrected_minutes": int(corrected_minutes),
                    "corrected_recap_credits": (
                        int(corrected_recap_credits)
                        if corrected_recap_credits is not None
                        else None
                    ),
                    "edited_by": admin_id,
                }
            ),
        )
    )
    await session.flush()
    return await approve_payment_request(session, payment_id, admin_id)
