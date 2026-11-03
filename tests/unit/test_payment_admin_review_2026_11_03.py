"""Batch I — payment admin review pattern.

Covers the multi-admin Telegram copy sync and the edit-and-approve
flow built on top of the shared payments service:

- a Telegram approve/reject credits (or not) exactly once and syncs
  every OTHER admin's stored copy (keyboard cleared + Burmese status
  line), tolerating a copy whose edit fails;
- a Mini App approve/reject syncs the Telegram copies too;
- the submission notification carries the three-action review
  keyboard and records each admin's copy on the payment row;
- edit-and-approve claims the request (a second admin is blocked),
  credits the CORRECTED minutes on confirm with audit fields, and
  releases the claim + restores keyboards on cancel;
- non-admin callbacks are rejected; double approval stays impossible.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Message, User as TgUser
from sqlalchemy import select

from app.config import settings
from app.handlers.admin import (
    PaymentReviewFSM,
    handle_admin_pay_approve_confirm,
    handle_admin_pay_approve_prompt,
    handle_admin_pay_edit_cancel,
    handle_admin_pay_edit_confirm,
    handle_admin_pay_edit_minutes_input,
    handle_admin_pay_edit_start,
    handle_admin_pay_reject_confirm,
)
from app.handlers.payment import _finalize_receipt_submission
from app.models.enums import PaymentStatus
from app.models.schema import AuditLog, PaymentRequest
from app.services.ledger import get_minute_balance, get_or_create_user
from app.services.payment_review import (
    claim_payment_review,
    parse_admin_copies,
    release_payment_claim,
)
from app.services.payments import approve_payment_request, create_payment_request

ADMIN1 = 9101
ADMIN2 = 9102
BUYER = 7001
ORIGINAL_TEXT = "💳 <b>New Payment Request (#PAY-X)</b>\n\nAmount: 8,000 MMK"


class FakeFSMContext:
    """In-memory FSMContext replica (same pattern as
    test_admin_user_management.py)."""

    def __init__(self, initial_state=None, initial_data=None):
        self.current_state = initial_state
        self.data = initial_data or {}

    async def set_state(self, state):
        self.current_state = state

    async def get_state(self):
        return self.current_state

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def clear(self):
        self.current_state = None
        self.data = {}


def _copies(*specs: tuple[int, int, str]) -> str:
    return json.dumps(
        [
            {"chat_id": chat_id, "message_id": mid, "kind": kind, "text": ORIGINAL_TEXT}
            for chat_id, mid, kind in specs
        ]
    )


async def _make_payment(db_session, pay_id: str, *, minutes: int = 10, copies: str | None = None):
    buyer, _ = await get_or_create_user(db_session, telegram_id=BUYER)
    payment = PaymentRequest(
        id=pay_id,
        user_id=buyer.id,
        package_code="MIN_10",
        minutes=minutes,
        amount_mmk=8000,
        receipt_file_id="slip-file-1",
        status=PaymentStatus.PENDING.value,
        admin_notify_copies=copies,
    )
    db_session.add(payment)
    await db_session.flush()
    return buyer, payment


def _admin_callback(admin_id: int, first_name: str, data: str, *, chat_id: int, message_id: int):
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=admin_id, is_bot=False, first_name=first_name)
    cb.data = data
    cb.bot = AsyncMock()
    cb.bot.send_message = AsyncMock()
    cb.bot.edit_message_text = AsyncMock()
    cb.bot.edit_message_caption = AsyncMock()
    msg = MagicMock(spec=Message)
    msg.chat = SimpleNamespace(id=chat_id)
    msg.message_id = message_id
    msg.photo = None
    msg.document = None
    msg.video = None
    msg.animation = None
    msg.audio = None
    msg.voice = None
    msg.caption = None
    msg.edit_text = AsyncMock()
    msg.edit_caption = AsyncMock()
    msg.answer = AsyncMock()
    cb.message = msg
    cb.answer = AsyncMock()
    return cb


def _admin_message(admin_id: int, first_name: str, text: str):
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=admin_id, is_bot=False, first_name=first_name)
    msg.text = text
    msg.answer = AsyncMock()
    msg.bot = AsyncMock()
    return msg


def _answered_alert(cb) -> str:
    """The text of the last alert-style callback.answer call."""
    assert cb.answer.call_args is not None
    return cb.answer.call_args[0][0] if cb.answer.call_args[0] else ""


# ── Telegram approve / reject sync ───────────────────────────────────


@pytest.mark.asyncio
async def test_telegram_approve_credits_once_and_syncs_all_copies(db_session):
    settings.admin_ids_raw = f"{ADMIN1},{ADMIN2}"
    buyer, payment = await _make_payment(
        db_session,
        "PAY-REV-1",
        copies=_copies(
            (ADMIN1, 101, "text"),   # actor's own copy — handler rewrites it, sync skips it
            (ADMIN2, 201, "text"),   # synced via edit_message_text
            (ADMIN2, 202, "photo"),  # synced via edit_message_caption
        ),
    )

    cb = _admin_callback(ADMIN1, "AdminOne", f"admin:pay_approve_confirm:{payment.id}", chat_id=ADMIN1, message_id=101)
    await handle_admin_pay_approve_confirm(cb, session=db_session)

    await db_session.refresh(payment)
    assert payment.status == PaymentStatus.APPROVED.value
    assert payment.reviewed_by == ADMIN1
    bal = await get_minute_balance(db_session, buyer.id)
    assert bal.purchased_seconds == 600  # 10 minutes, credited exactly once

    # The other admin's TEXT copy: keyboard cleared + status line.
    text_calls = cb.bot.edit_message_text.call_args_list
    assert len(text_calls) == 1
    kwargs = text_calls[0].kwargs
    assert kwargs["chat_id"] == ADMIN2 and kwargs["message_id"] == 201
    assert kwargs["reply_markup"] is None
    assert kwargs["text"].startswith(ORIGINAL_TEXT)
    assert "✅ AdminOne က အတည်ပြုပြီးပါပြီ" in kwargs["text"]

    # The other admin's PHOTO copy: caption edited the same way.
    caption_calls = cb.bot.edit_message_caption.call_args_list
    assert len(caption_calls) == 1
    kwargs = caption_calls[0].kwargs
    assert kwargs["chat_id"] == ADMIN2 and kwargs["message_id"] == 202
    assert kwargs["reply_markup"] is None
    assert "✅ AdminOne က အတည်ပြုပြီးပါပြီ" in kwargs["caption"]

    # Double approval through the service stays impossible.
    ok, _, msg = await approve_payment_request(db_session, payment.id, admin_id=ADMIN2)
    assert ok is False and msg == "ALREADY_APPROVED"
    bal = await get_minute_balance(db_session, buyer.id)
    assert bal.purchased_seconds == 600


@pytest.mark.asyncio
async def test_telegram_approve_sync_failure_is_tolerated(db_session):
    """One admin's copy being too old/deleted must not fail approval."""
    settings.admin_ids_raw = f"{ADMIN1},{ADMIN2}"
    buyer, payment = await _make_payment(
        db_session, "PAY-REV-2", copies=_copies((ADMIN2, 201, "text"))
    )
    cb = _admin_callback(ADMIN1, "AdminOne", f"admin:pay_approve_confirm:{payment.id}", chat_id=ADMIN1, message_id=101)
    cb.bot.edit_message_text = AsyncMock(side_effect=Exception("message to edit not found"))

    await handle_admin_pay_approve_confirm(cb, session=db_session)

    await db_session.refresh(payment)
    assert payment.status == PaymentStatus.APPROVED.value
    bal = await get_minute_balance(db_session, buyer.id)
    assert bal.purchased_seconds == 600


@pytest.mark.asyncio
async def test_telegram_reject_syncs_all_copies(db_session):
    settings.admin_ids_raw = f"{ADMIN1},{ADMIN2}"
    buyer, payment = await _make_payment(
        db_session,
        "PAY-REV-3",
        copies=_copies((ADMIN1, 101, "text"), (ADMIN2, 201, "text")),
    )
    cb = _admin_callback(ADMIN1, "AdminOne", f"admin:pay_reject_confirm:{payment.id}", chat_id=ADMIN1, message_id=101)
    await handle_admin_pay_reject_confirm(cb, session=db_session)

    await db_session.refresh(payment)
    assert payment.status == PaymentStatus.REJECTED.value
    bal = await get_minute_balance(db_session, buyer.id)
    assert bal.purchased_seconds == 0

    text_calls = cb.bot.edit_message_text.call_args_list
    assert len(text_calls) == 1
    kwargs = text_calls[0].kwargs
    assert kwargs["chat_id"] == ADMIN2 and kwargs["message_id"] == 201
    assert kwargs["reply_markup"] is None
    assert "🚫 AdminOne က ငြင်းပယ်ပြီးပါပြီ" in kwargs["text"]


@pytest.mark.asyncio
async def test_double_approve_via_telegram_impossible(db_session):
    settings.admin_ids_raw = f"{ADMIN1},{ADMIN2}"
    buyer, payment = await _make_payment(db_session, "PAY-REV-4")

    cb1 = _admin_callback(ADMIN1, "AdminOne", f"admin:pay_approve_confirm:{payment.id}", chat_id=ADMIN1, message_id=101)
    await handle_admin_pay_approve_confirm(cb1, session=db_session)

    cb2 = _admin_callback(ADMIN2, "AdminTwo", f"admin:pay_approve_confirm:{payment.id}", chat_id=ADMIN2, message_id=201)
    await handle_admin_pay_approve_confirm(cb2, session=db_session)
    assert "ALREADY_APPROVED" in _answered_alert(cb2)

    bal = await get_minute_balance(db_session, buyer.id)
    assert bal.purchased_seconds == 600


# ── Mini App actions sync the Telegram copies ─────────────────────────


def _fake_bot_class():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.edit_message_text = AsyncMock()
    bot.edit_message_caption = AsyncMock()
    bot.session = SimpleNamespace(close=AsyncMock())
    return MagicMock(return_value=bot), bot


@pytest.mark.asyncio
async def test_miniapp_approve_syncs_telegram_copies(db_session):
    from app.webapp.api import ApproveSlipRequest, approve_payment_slip

    settings.admin_ids_raw = f"{ADMIN1},{ADMIN2}"
    settings.bot_token = "123456:fake-token"
    admin_user, _ = await get_or_create_user(db_session, telegram_id=ADMIN1)
    admin_user.first_name = "Boss"
    buyer, payment = await _make_payment(
        db_session,
        "PAY-REV-5",
        copies=_copies((ADMIN1, 101, "text"), (ADMIN2, 201, "photo")),
    )

    bot_cls, fake_bot = _fake_bot_class()
    with patch("aiogram.Bot", bot_cls):
        result = await approve_payment_slip(
            ApproveSlipRequest(request_id=payment.id), admin=admin_user, db=db_session
        )

    assert result["success"] is True
    await db_session.refresh(payment)
    assert payment.status == PaymentStatus.APPROVED.value
    bal = await get_minute_balance(db_session, buyer.id)
    assert bal.purchased_seconds == 600

    # BOTH copies synced (the Mini App path skips nobody).
    edited = {
        (c.kwargs["chat_id"], c.kwargs["message_id"]) for c in fake_bot.edit_message_text.call_args_list
    }
    assert (ADMIN1, 101) in edited
    text_kwargs = fake_bot.edit_message_text.call_args_list[0].kwargs
    assert text_kwargs["reply_markup"] is None
    assert "✅ Boss က အတည်ပြုပြီးပါပြီ" in text_kwargs["text"]
    caption_kwargs = fake_bot.edit_message_caption.call_args_list[0].kwargs
    assert (caption_kwargs["chat_id"], caption_kwargs["message_id"]) == (ADMIN2, 201)
    assert caption_kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_miniapp_reject_syncs_telegram_copies(db_session):
    from app.webapp.api import RejectSlipRequest, reject_payment_slip

    settings.admin_ids_raw = f"{ADMIN1},{ADMIN2}"
    settings.bot_token = "123456:fake-token"
    admin_user, _ = await get_or_create_user(db_session, telegram_id=ADMIN2)
    admin_user.first_name = "Second"
    buyer, payment = await _make_payment(
        db_session, "PAY-REV-6", copies=_copies((ADMIN1, 101, "text"))
    )

    bot_cls, fake_bot = _fake_bot_class()
    with patch("aiogram.Bot", bot_cls):
        result = await reject_payment_slip(
            RejectSlipRequest(request_id=payment.id, reason="ပြေစာ မမှန်ကန်ပါ"),
            admin=admin_user,
            db=db_session,
        )

    assert result["success"] is True
    await db_session.refresh(payment)
    assert payment.status == PaymentStatus.REJECTED.value
    bal = await get_minute_balance(db_session, buyer.id)
    assert bal.purchased_seconds == 0
    kwargs = fake_bot.edit_message_text.call_args_list[0].kwargs
    assert kwargs["reply_markup"] is None
    assert "🚫 Second က ငြင်းပယ်ပြီးပါပြီ" in kwargs["text"]


# ── Submission notification: keyboard + recorded copies ──────────────


@pytest.mark.asyncio
async def test_notification_records_copies_and_review_keyboard(db_session):
    settings.admin_ids_raw = f"{ADMIN1},{ADMIN2}"
    buyer, _ = await get_or_create_user(db_session, telegram_id=BUYER)
    payment = await create_payment_request(
        session=db_session, user_id=buyer.id, package_code="MIN_10", receipt_file_id="slip-9"
    )
    await db_session.commit()

    message = MagicMock(spec=Message)
    message.from_user = TgUser(id=BUYER, is_bot=False, first_name="Buyer", username="buyer")
    message.photo = [SimpleNamespace(file_id="slip-9")]
    message.document = None
    message.answer = AsyncMock()
    bot = AsyncMock()

    async def _send_photo(**kwargs):
        return SimpleNamespace(message_id=5000 + kwargs["chat_id"])

    bot.send_photo = AsyncMock(side_effect=_send_photo)
    message.bot = bot

    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    with patch("app.handlers.payment.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await _finalize_receipt_submission(message, payment, "slip-9", "my")

    assert bot.send_photo.await_count == 2
    kb = bot.send_photo.call_args_list[0].kwargs["reply_markup"]
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    datas = {btn.callback_data for btn in buttons}
    assert f"admin:pay_approve:{payment.id}" in datas
    assert f"admin:pay_reject:{payment.id}" in datas
    assert f"admin:pay_edit:{payment.id}" in datas
    texts = {btn.text for btn in buttons}
    assert "✅ အတည်ပြုမယ်" in texts
    assert "🚫 ငြင်းမယ်" in texts
    assert "✏️ ပြင်ပြီးအတည်ပြုမယ်" in texts

    await db_session.refresh(payment)
    copies = parse_admin_copies(payment)
    assert len(copies) == 2
    assert {c["chat_id"] for c in copies} == {ADMIN1, ADMIN2}
    assert all(c["kind"] == "photo" for c in copies)
    assert {c["message_id"] for c in copies} == {5000 + ADMIN1, 5000 + ADMIN2}


# ── Edit-and-approve ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_edit_claim_blocks_second_admin_and_confirm_credits_corrected(db_session):
    settings.admin_ids_raw = f"{ADMIN1},{ADMIN2}"
    buyer, payment = await _make_payment(
        db_session,
        "PAY-REV-7",
        minutes=10,
        copies=_copies((ADMIN1, 101, "text"), (ADMIN2, 201, "text")),
    )
    fsm1 = FakeFSMContext()

    # Admin 1 taps Edit → claim held, Admin 2's copy keyboard cleared.
    cb1 = _admin_callback(ADMIN1, "AdminOne", f"admin:pay_edit:{payment.id}", chat_id=ADMIN1, message_id=101)
    await handle_admin_pay_edit_start(cb1, fsm1, session=db_session)
    await db_session.refresh(payment)
    assert payment.review_claimed_by == ADMIN1
    assert payment.status == PaymentStatus.PENDING.value
    assert fsm1.current_state == PaymentReviewFSM.waiting_for_corrected_minutes
    cb1.message.answer.assert_awaited()  # FSM prompt sent to the claimer
    claimed_calls = cb1.bot.edit_message_text.call_args_list
    assert len(claimed_calls) == 1
    kwargs = claimed_calls[0].kwargs
    assert kwargs["chat_id"] == ADMIN2 and kwargs["reply_markup"] is None
    assert "AdminOne က ပြင်ဆင်နေပါသည်" in kwargs["text"]

    # Admin 2 tapping Edit is blocked by the claim.
    fsm2 = FakeFSMContext()
    cb2 = _admin_callback(ADMIN2, "AdminTwo", f"admin:pay_edit:{payment.id}", chat_id=ADMIN2, message_id=201)
    await handle_admin_pay_edit_start(cb2, fsm2, session=db_session)
    assert "ကိုင်တွယ်နေပါပြီ" in _answered_alert(cb2)
    assert cb2.answer.call_args.kwargs.get("show_alert") is True
    await db_session.refresh(payment)
    assert payment.review_claimed_by == ADMIN1

    # Admin 2 tapping Approve during the claim is blocked too.
    cb2b = _admin_callback(ADMIN2, "AdminTwo", f"admin:pay_approve:{payment.id}", chat_id=ADMIN2, message_id=201)
    await handle_admin_pay_approve_prompt(cb2b, session=db_session)
    assert "ကိုင်တွယ်နေပါပြီ" in _answered_alert(cb2b)
    await db_session.refresh(payment)
    assert payment.status == PaymentStatus.PENDING.value

    # Admin 1 enters the corrected minutes → confirmation prompt.
    msg = _admin_message(ADMIN1, "AdminOne", "25")
    await handle_admin_pay_edit_minutes_input(msg, fsm1, session=db_session)
    assert fsm1.current_state == PaymentReviewFSM.confirming_edit
    assert fsm1.data["corrected_minutes"] == 25
    confirm_text = msg.answer.call_args[0][0]
    assert "10" in confirm_text and "25" in confirm_text

    # Admin 1 confirms → corrected approval through the shared service.
    cb1b = _admin_callback(ADMIN1, "AdminOne", f"admin:pay_edit_confirm:{payment.id}", chat_id=ADMIN1, message_id=101)
    await handle_admin_pay_edit_confirm(cb1b, fsm1, session=db_session)

    await db_session.refresh(payment)
    assert payment.status == PaymentStatus.APPROVED.value
    assert payment.minutes == 25
    assert payment.original_minutes == 10
    assert payment.edited_by == ADMIN1
    assert payment.review_claimed_by is None  # released by the terminal transition
    assert fsm1.current_state is None

    bal = await get_minute_balance(db_session, buyer.id)
    assert bal.purchased_seconds == 25 * 60  # the CORRECTED amount, exactly once

    actions = {
        row.action
        for row in (await db_session.execute(select(AuditLog).where(AuditLog.target_id == payment.id))).scalars().all()
    }
    assert "payment.edit_approve" in actions
    assert "payment.approve" in actions

    # ALL copies (including the claimer's own) got the status line.
    sync_calls = cb1b.bot.edit_message_text.call_args_list
    assert len(sync_calls) == 2
    for call in sync_calls:
        assert call.kwargs["reply_markup"] is None
        assert "AdminOne က အတည်ပြုပြီးပါပြီ" in call.kwargs["text"]
        assert "10 → 25" in call.kwargs["text"]


@pytest.mark.asyncio
async def test_edit_cancel_releases_claim_and_restores_keyboards(db_session):
    settings.admin_ids_raw = f"{ADMIN1},{ADMIN2}"
    buyer, payment = await _make_payment(
        db_session, "PAY-REV-8", copies=_copies((ADMIN1, 101, "text"), (ADMIN2, 201, "text"))
    )
    fsm1 = FakeFSMContext()
    cb1 = _admin_callback(ADMIN1, "AdminOne", f"admin:pay_edit:{payment.id}", chat_id=ADMIN1, message_id=101)
    await handle_admin_pay_edit_start(cb1, fsm1, session=db_session)
    await db_session.refresh(payment)
    assert payment.review_claimed_by == ADMIN1

    # Release at the service level belongs to the claimer only.
    assert await release_payment_claim(db_session, payment.id, ADMIN2) is False
    await db_session.refresh(payment)
    assert payment.review_claimed_by == ADMIN1

    cb_cancel = _admin_callback(ADMIN1, "AdminOne", f"admin:pay_edit_cancel:{payment.id}", chat_id=ADMIN1, message_id=101)
    await handle_admin_pay_edit_cancel(cb_cancel, fsm1, session=db_session)

    await db_session.refresh(payment)
    assert payment.review_claimed_by is None
    assert payment.status == PaymentStatus.PENDING.value
    assert fsm1.current_state is None

    # Admin 2's copy got its original text + a fresh review keyboard.
    restore_calls = cb_cancel.bot.edit_message_text.call_args_list
    assert len(restore_calls) == 1
    kwargs = restore_calls[0].kwargs
    assert kwargs["chat_id"] == ADMIN2 and kwargs["message_id"] == 201
    assert kwargs["text"] == ORIGINAL_TEXT
    assert kwargs["reply_markup"] is not None
    datas = {
        btn.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for btn in row
    }
    assert f"admin:pay_approve:{payment.id}" in datas
    assert f"admin:pay_edit:{payment.id}" in datas

    # And Admin 2 can now claim the request.
    fsm2 = FakeFSMContext()
    cb2 = _admin_callback(ADMIN2, "AdminTwo", f"admin:pay_edit:{payment.id}", chat_id=ADMIN2, message_id=201)
    await handle_admin_pay_edit_start(cb2, fsm2, session=db_session)
    await db_session.refresh(payment)
    assert payment.review_claimed_by == ADMIN2


@pytest.mark.asyncio
async def test_claim_service_codes(db_session):
    buyer, payment = await _make_payment(db_session, "PAY-REV-9")
    ok, _, code = await claim_payment_review(db_session, payment.id, ADMIN1)
    assert ok is True and code == "CLAIMED"
    ok, _, code = await claim_payment_review(db_session, payment.id, ADMIN2)
    assert ok is False and code == "CLAIMED_BY_OTHER"
    # The claimer re-tapping Edit re-enters cleanly.
    ok, _, code = await claim_payment_review(db_session, payment.id, ADMIN1)
    assert ok is True and code == "CLAIMED"


# ── Authorization ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_admin_callbacks_rejected(db_session):
    settings.admin_ids_raw = f"{ADMIN1}"
    buyer, payment = await _make_payment(db_session, "PAY-REV-10", copies=_copies((ADMIN1, 101, "text")))
    intruder = 9999

    fsm = FakeFSMContext()
    cb_edit = _admin_callback(intruder, "Mallory", f"admin:pay_edit:{payment.id}", chat_id=intruder, message_id=1)
    await handle_admin_pay_edit_start(cb_edit, fsm, session=db_session)
    assert cb_edit.answer.call_args.kwargs.get("show_alert") is True
    await db_session.refresh(payment)
    assert payment.review_claimed_by is None
    assert fsm.current_state is None

    cb_approve = _admin_callback(intruder, "Mallory", f"admin:pay_approve_confirm:{payment.id}", chat_id=intruder, message_id=1)
    await handle_admin_pay_approve_confirm(cb_approve, session=db_session)
    await db_session.refresh(payment)
    assert payment.status == PaymentStatus.PENDING.value
    bal = await get_minute_balance(db_session, buyer.id)
    assert bal.purchased_seconds == 0
