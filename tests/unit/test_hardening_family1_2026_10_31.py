"""Hardening batch — Family 1 (bot trust edges): B1, B3, F12, F13.

B1: bot-blocked != admin-banned. mark_user_blocked sets ONLY the new
    bot_blocked flag; broadcasts skip flagged users; the flag clears on
    the next inbound interaction; status=BLOCKED remains admin-only.
B3: CVR assign/reject are locked + PENDING-only; terminal requests are
    refused, and reject-after-approve can no longer free a slot while
    the grant stays alive.
F12: unknown buy_pkg code rejected (no phantom 10min/2000 invoice);
    menu:buy_minutes clears FSM state; non-photo content in
    waiting_for_receipt gets guidance.
F13: payment request IDs carry 12 hex chars.
"""
from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message, User as TgUser

from app.config import settings
from app.middlewares import AccessControlMiddleware
from app.models.enums import AccountCategory, CustomVoiceRequestStatus, UserStatus
from app.models.schema import User, UserVoiceModelGrant, VoiceModel
from app.services.broadcast import get_broadcast_recipient_ids, mark_user_blocked
from app.services.custom_voice import (
    assign_model_to_request,
    create_custom_voice_request,
    reject_custom_voice_request,
)
from app.services.ledger import get_or_create_user
from app.services.payments import create_payment_request, get_active_pricing_plans


def _session_maker(db_session):
    maker = MagicMock()
    maker.return_value.__aenter__ = AsyncMock(return_value=db_session)
    maker.return_value.__aexit__ = AsyncMock(return_value=None)
    return maker


# ── B1 ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_b1_mark_user_blocked_sets_flag_only(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=7101)
    await db_session.commit()

    await mark_user_blocked(db_session, 7101)

    refreshed = await db_session.get(User, 7101)
    assert refreshed.bot_blocked is True
    assert refreshed.status == UserStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_b1_broadcast_recipients_exclude_bot_blocked(db_session):
    u_ok, _ = await get_or_create_user(db_session, telegram_id=7102)
    u_flag, _ = await get_or_create_user(db_session, telegram_id=7103)
    u_flag.bot_blocked = True
    u_ban, _ = await get_or_create_user(db_session, telegram_id=7104)
    u_ban.status = UserStatus.BLOCKED.value
    await db_session.commit()

    ids = await get_broadcast_recipient_ids(db_session, "REGISTERED_ALL")
    assert 7102 in ids
    assert 7103 not in ids  # send-suppressed by flag
    assert 7104 not in ids  # admin-banned


@pytest.mark.asyncio
async def test_b1_middleware_clears_flag_on_inbound(db_session):
    settings.admin_ids_raw = "999001"
    db_user = User(
        id=7105, username="flagged", first_name="Flagged",
        language_code="my", account_category=AccountCategory.FREE.value,
        status=UserStatus.ACTIVE.value, bot_blocked=True,
    )
    db_session.add(db_user)
    await db_session.commit()

    middleware = AccessControlMiddleware(session_factory=_session_maker(db_session))
    handler = AsyncMock(return_value="OK")
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=7105, is_bot=False, first_name="Flagged")
    msg.answer = AsyncMock()

    result = await middleware(handler, msg, {})

    assert result == "OK"
    handler.assert_called_once()
    refreshed = await db_session.get(User, 7105)
    await db_session.refresh(refreshed)
    assert refreshed.bot_blocked is False
    assert refreshed.status == UserStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_b1_middleware_flag_alone_never_blocks(db_session):
    """A bot_blocked user with ACTIVE status gets through (no suspension)."""
    settings.admin_ids_raw = "999001"
    db_user = User(
        id=7106, username="flagged2", first_name="Flagged2",
        language_code="en", account_category=AccountCategory.FREE.value,
        status=UserStatus.ACTIVE.value, bot_blocked=True,
    )
    db_session.add(db_user)
    await db_session.commit()

    middleware = AccessControlMiddleware(session_factory=_session_maker(db_session))
    handler = AsyncMock(return_value="OK")
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=7106, is_bot=False, first_name="Flagged2")
    cb.answer = AsyncMock()

    result = await middleware(handler, cb, {})
    assert result == "OK"
    # Suspension notice must NOT be sent for a mere bot_block flag.
    for call in cb.answer.call_args_list:
        assert "suspended" not in str(call).lower()


# ── B3 ────────────────────────────────────────────────────────────────

_MODEL_SEQ = [7800]


async def _make_model(db_session, name="Model B3"):
    _MODEL_SEQ[0] += 1
    model = VoiceModel(
        id=_MODEL_SEQ[0],
        name=name,
        slug=f"b3-model-{_MODEL_SEQ[0]}",
        model_dir="storage/models/b3",
        # Runnable marker: hardening V1 refuses non-"voxcpm" models at
        # assign; B3's subject is the PENDING guard, not the format.
        pth_path="voxcpm",
        type="SYSTEM",
        status="ACTIVE",
    )
    db_session.add(model)
    await db_session.flush()
    return model


async def _make_request(db_session, user_id=7201):
    user, _ = await get_or_create_user(db_session, telegram_id=user_id)
    user.account_category = AccountCategory.PURCHASED.value
    await db_session.flush()
    ok, req, msg = await create_custom_voice_request(
        session=db_session,
        user_id=user_id,
        model_name="My Voice",
        sample_file_id="sample-file-id",
    )
    assert ok, msg
    await db_session.flush()
    return req


@pytest.mark.asyncio
async def test_b3_double_assign_refused(db_session):
    model = await _make_model(db_session)
    req = await _make_request(db_session)

    ok, _, msg = await assign_model_to_request(db_session, req.id, model.id, admin_id=1)
    assert ok and msg == "MODEL_ASSIGNED"
    await db_session.flush()

    ok2, req2, msg2 = await assign_model_to_request(db_session, req.id, model.id, admin_id=1)
    assert ok2 is False
    assert msg2.startswith("REQUEST_NOT_PENDING")
    assert req2.status == CustomVoiceRequestStatus.APPROVED.value

    grants = (
        await db_session.execute(
            UserVoiceModelGrant.__table__.select().where(
                UserVoiceModelGrant.user_id == 7201
            )
        )
    ).fetchall()
    assert len(grants) == 1  # exactly one grant, no duplicate


@pytest.mark.asyncio
async def test_b3_reject_after_approve_refused_and_grant_intact(db_session):
    model = await _make_model(db_session)
    req = await _make_request(db_session, user_id=7202)

    ok, _, _ = await assign_model_to_request(db_session, req.id, model.id, admin_id=1)
    assert ok
    await db_session.flush()

    ok2, req2, msg2 = await reject_custom_voice_request(
        db_session, req.id, admin_id=1, reason="changed my mind"
    )
    assert ok2 is False
    assert msg2.startswith("REQUEST_NOT_PENDING")
    # Status and grant both survive — the slot-cap defeat is closed.
    assert req2.status == CustomVoiceRequestStatus.APPROVED.value
    grant = (
        await db_session.execute(
            UserVoiceModelGrant.__table__.select().where(
                UserVoiceModelGrant.user_id == 7202
            )
        )
    ).fetchall()
    assert len(grant) == 1


@pytest.mark.asyncio
async def test_b3_assign_after_reject_refused(db_session):
    req = await _make_request(db_session, user_id=7203)
    ok, _, msg = await reject_custom_voice_request(
        db_session, req.id, admin_id=1, reason="bad sample"
    )
    assert ok and msg == "REQUEST_REJECTED"
    await db_session.flush()

    model = await _make_model(db_session)
    ok2, _, msg2 = await assign_model_to_request(db_session, req.id, model.id, admin_id=1)
    assert ok2 is False
    assert msg2.startswith("REQUEST_NOT_PENDING")


# ── F12 ───────────────────────────────────────────────────────────────

class _FakeState:
    def __init__(self, data=None):
        self.data = data or {}
        self.current_state = None
        self.cleared = False

    async def set_state(self, state):
        self.current_state = state

    async def get_state(self):
        return self.current_state

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def clear(self):
        self.cleared = True
        self.current_state = None
        self.data = {}


def _fake_callback(uid=7301, data="buy_pkg:NOPE"):
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=uid, is_bot=False, first_name="Buyer")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    cb.data = data
    return cb


@pytest.mark.asyncio
async def test_f12_unknown_package_rejected_no_phantom_invoice(db_session, monkeypatch):
    from contextlib import asynccontextmanager

    from app.handlers import payment as payment_mod

    await get_active_pricing_plans(db_session)  # seed DB plans
    user, _ = await get_or_create_user(db_session, telegram_id=7301)
    await db_session.commit()

    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    monkeypatch.setattr(payment_mod, "AsyncSessionLocal", lambda: mock_session_ctx())

    cb = _fake_callback()
    state = _FakeState()
    await payment_mod.handle_package_selected(cb, state=state)

    # Alert shown, NO invoice rendered, NO FSM state entered.
    cb.answer.assert_called_once()
    assert cb.answer.call_args[1].get("show_alert") is True
    cb.message.edit_text.assert_not_called()
    assert state.current_state is None
    assert state.data == {}


@pytest.mark.asyncio
async def test_f12_buy_minutes_menu_clears_state(db_session, monkeypatch):
    from contextlib import asynccontextmanager

    from app.handlers import payment as payment_mod

    await get_active_pricing_plans(db_session)
    user, _ = await get_or_create_user(db_session, telegram_id=7302)
    await db_session.commit()

    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    monkeypatch.setattr(payment_mod, "AsyncSessionLocal", lambda: mock_session_ctx())

    cb = _fake_callback(data="menu:buy_minutes")
    state = _FakeState(data={"package_code": "MIN_10"})
    state.current_state = "PaymentFSM:waiting_for_receipt"

    await payment_mod.handle_buy_minutes_menu(cb, state=state)

    assert state.cleared is True
    assert state.current_state is None


@pytest.mark.asyncio
async def test_f12_wrong_content_in_receipt_state_gets_guidance():
    from app.handlers.payment import handle_receipt_wrong_content

    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=7303, is_bot=False, first_name="Buyer")
    msg.answer = AsyncMock()
    state = _FakeState(data={"lang": "en"})

    await handle_receipt_wrong_content(msg, state=state)

    msg.answer.assert_called_once()
    assert "screenshot" in msg.answer.call_args[0][0]
    # State untouched: the slip can still be sent afterwards.
    assert state.cleared is False


# ── F13 ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_f13_payment_id_has_12_hex_chars(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=7401)
    await db_session.flush()

    payment = await create_payment_request(
        session=db_session,
        user_id=7401,
        package_code="MIN_10",
        receipt_file_id="file-id",
    )
    assert re.fullmatch(r"PAY-\d{8}-[0-9A-F]{12}", payment.id), payment.id
