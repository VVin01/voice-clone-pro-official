"""Bot-chat cleanup 2026-11-03 — new behaviour pins.

Covers: lean main-menu shape + membership-button visibility, the
membership status card, language screen Back + active marker,
broadcast 0-audience hiding + test-send, the admin pulse failure
class, legacy-model display filtering (display-only — authorization
semantics unchanged), the payment store tier/balance header, the
post-restart receipt text hint, and sanitized send-failure copy.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Message, User as TgUser

from app.config import settings
from app.handlers.admin import (
    _failure_class,
    handle_admin_pay_receipt,
)
from app.handlers.group_admin import handle_private_message
from app.handlers.menu import render_main_menu
from app.handlers.payment import handle_buy_minutes_menu
from app.services import group_ai_admin as gai
from app.services import private_ai as pai
from app.models.enums import AccountCategory, PaymentStatus
from app.models.schema import PaymentRequest, User, VoiceModel
from app.services.i18n import t
from app.services.ledger import get_or_create_user
from app.services.models import (
    get_available_models_for_user,
    get_displayable_models_for_user,
    is_displayable_model,
    is_legacy_local_model,
)
from app.services.payments import save_payment_intent
from app.ui.keyboards import (
    admin_broadcast_audience_kb,
    language_selection_kb,
    main_menu_kb,
)


class FakeFSMContext:
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


def _flat_buttons(kb):
    return [btn for row in kb.inline_keyboard for btn in row]


def _callbacks(kb):
    return [btn.callback_data for btn in _flat_buttons(kb) if btn.callback_data]


# ---------------------------------------------------------------------------
# Main menu shape (updated to Round 13 Part K 3-button contract)
# ---------------------------------------------------------------------------

def test_main_menu_shape_verified_user():
    kb = main_menu_kb(is_admin=False, lang="my")
    buttons = _flat_buttons(kb)
    # Round 13 Part K: exact 3 buttons — Open Mini App, Refer Friends, Change Language.
    assert len(buttons) == 3
    assert buttons[0].web_app is not None
    assert "tab=" not in (buttons[0].web_app.url or "")
    assert _callbacks(kb) == ["menu:refer_earn", "menu:change_lang"]
    assert not any(b.web_app and "tab=" in (b.web_app.url or "") for b in buttons)
    assert "gate:verify" not in _callbacks(kb)
    assert "menu:membership" not in _callbacks(kb)


def test_main_menu_shape_membership_pending_and_admin():
    # Round 13 Part K: main_menu_kb is identical (3 buttons) regardless of pending/admin flags.
    kb = main_menu_kb(is_admin=False, lang="en", membership_pending=True)
    assert len(_flat_buttons(kb)) == 3
    assert _callbacks(kb) == ["menu:refer_earn", "menu:change_lang"]

    kb_admin = main_menu_kb(is_admin=True, lang="en")
    assert len(_flat_buttons(kb_admin)) == 3
    assert _callbacks(kb_admin) == ["menu:refer_earn", "menu:change_lang"]


@pytest.mark.asyncio
async def test_render_main_menu_returns_welcome_and_3_buttons(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=9101)
    await db_session.flush()
    text, kb = await render_main_menu(user.id, lang="en", session=db_session)
    assert "Welcome to Voice Clone Pro" in text
    assert _callbacks(kb) == ["menu:refer_earn", "menu:change_lang"]


# ---------------------------------------------------------------------------
# Language screen
# ---------------------------------------------------------------------------

def test_language_selection_kb_back_and_active_marker():
    # Round 13 Part K removed menu:home back row; language picker shows active check + 2 language buttons.
    kb = language_selection_kb(active_lang="my", show_back=True, lang="my")
    buttons = _flat_buttons(kb)
    assert buttons[0].text.startswith("✅")
    assert not buttons[1].text.startswith("✅")
    assert _callbacks(kb) == ["set_lang:my", "set_lang:en"]

    kb_plain = language_selection_kb()
    assert _callbacks(kb_plain) == ["set_lang:my", "set_lang:en"]
    assert not any(b.text.startswith("✅") for b in _flat_buttons(kb_plain))


# ---------------------------------------------------------------------------
# Broadcast: 0-audience hiding (bot-chat broadcast FSM retired in Round 13 Part K)
# ---------------------------------------------------------------------------

def test_broadcast_audience_kb_hides_zero_counts():
    kb = admin_broadcast_audience_kb(
        registered_count=12, imported_all_count=0, imported_unregistered_count=0, lang="en"
    )
    cbs = _callbacks(kb)
    assert "admin:bc_group:REGISTERED_ALL" in cbs
    assert "admin:bc_group:IMPORTED_ALL" not in cbs
    assert "admin:bc_group:IMPORTED_UNREGISTERED" not in cbs
    assert cbs[-1] == "admin:broadcast_cancel"

    kb_all = admin_broadcast_audience_kb(
        registered_count=1, imported_all_count=2, imported_unregistered_count=3, lang="en"
    )
    assert "admin:bc_group:IMPORTED_UNREGISTERED" in _callbacks(kb_all)


# ---------------------------------------------------------------------------
# Admin pulse failure class
# ---------------------------------------------------------------------------

def test_failure_class_extraction():
    assert _failure_class("INSUFFICIENT_BALANCE: user 5 lacks minutes") == "INSUFFICIENT_BALANCE"
    assert _failure_class("MODEL_NOT_AUTHORIZED") == "MODEL_NOT_AUTHORIZED"
    assert _failure_class(None) is None
    assert _failure_class("") is None
    out = _failure_class("worker died <badly> & gone")
    assert "&lt;badly&gt;" in out and "<badly>" not in out


# ---------------------------------------------------------------------------
# Legacy voice models: display filter, authorization untouched
# ---------------------------------------------------------------------------

def _mk_model(name, slug, mtype, pth, **kw):
    return VoiceModel(
        name=name,
        slug=slug,
        type=mtype,
        model_dir="cloud" if pth == "voxcpm" else "/models",
        pth_path=pth,
        status="ACTIVE",
        **kw,
    )


def test_is_legacy_local_model_truth_table():
    vox = _mk_model("VoxCPM", "voxcpm-cloud-ai", "SYSTEM", "voxcpm")
    legacy = _mk_model("The Light", "the-light", "SYSTEM", "/models/thelight/model.pth")
    personal = _mk_model("Mine", "mine", "PERSONAL", "/models/mine/model.pth", owner_user_id=1)
    assert is_legacy_local_model(vox) is False
    assert is_legacy_local_model(legacy) is True
    assert is_legacy_local_model(personal) is False
    assert is_displayable_model(vox) is True
    assert is_displayable_model(legacy) is False


@pytest.mark.asyncio
async def test_displayable_models_hide_legacy_but_authorization_unchanged(db_session):
    free_user, _ = await get_or_create_user(db_session, telegram_id=9201)
    await db_session.flush()
    vox = _mk_model("VoxCPM", "voxcpm-cloud-ai", "SYSTEM", "voxcpm",
                    allowed_for_free=True, allowed_for_purchased=True, is_default_free=True)
    legacy = _mk_model("Kartoon", "kartoon", "SYSTEM", "/models/kartoon/model.pth",
                       allowed_for_free=True, allowed_for_purchased=True)
    db_session.add_all([vox, legacy])
    await db_session.flush()

    available = await get_available_models_for_user(db_session, free_user.id)
    assert legacy.id in [m.id for m in available]  # authorization source unchanged

    displayable = await get_displayable_models_for_user(db_session, free_user.id)
    ids = [m.id for m in displayable]
    assert vox.id in ids
    assert legacy.id not in ids


@pytest.mark.asyncio
async def test_api_voice_models_list_hides_legacy(db_session):
    from app.webapp.api import list_voice_models

    user = User(id=9202, account_category=AccountCategory.FREE.value, language_code="en")
    db_session.add(user)
    vox = _mk_model("VoxCPM", "voxcpm-cloud-ai", "SYSTEM", "voxcpm",
                    allowed_for_free=True, allowed_for_purchased=True, is_default_free=True)
    legacy = _mk_model("Ant", "ant", "SYSTEM", "/models/ant/model.pth",
                       allowed_for_free=True, allowed_for_purchased=True)
    db_session.add_all([vox, legacy])
    await db_session.flush()

    out = await list_voice_models(user=user, db=db_session)
    slugs = [m["slug"] for m in out]
    assert "voxcpm-cloud-ai" in slugs
    assert "ant" not in slugs


# ---------------------------------------------------------------------------
# Payment store header + post-restart receipt hint
# ---------------------------------------------------------------------------

def _buy_menu_cb(user_id):
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=user_id, is_bot=False, first_name="Buyer")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    return cb


@pytest.mark.asyncio
async def test_buy_menu_header_free_vs_vip(db_session):
    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    free_user, _ = await get_or_create_user(db_session, telegram_id=9301)
    free_user.language_code = "en"
    vip_user, _ = await get_or_create_user(db_session, telegram_id=9302)
    vip_user.language_code = "en"
    vip_user.account_category = AccountCategory.PURCHASED.value
    await db_session.flush()

    state = FakeFSMContext()
    with patch("app.handlers.payment.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        cb_free = _buy_menu_cb(9301)
        await handle_buy_minutes_menu(cb_free, state)
        free_text = cb_free.message.edit_text.call_args[0][0]
        assert "Balance:" in free_text
        assert "Buy a Plan" in free_text
        assert "Top Up a Plan" not in free_text

        cb_vip = _buy_menu_cb(9302)
        await handle_buy_minutes_menu(cb_vip, state)
        vip_text = cb_vip.message.edit_text.call_args[0][0]
        assert "Balance:" in vip_text
        assert "Top Up a Plan" in vip_text
        # The Free->VIP pitch is a dead end for a VIP; it must not show.
        assert "Buying moves you from Free to VIP" not in vip_text
        # Pricing v3 (2026-11-23): 10 official catalogue plans.
        kb = cb_vip.message.edit_text.call_args[1]["reply_markup"]
        buy_cbs = [c for c in _callbacks(kb) if c.startswith("buy_pkg:")]
        assert len(buy_cbs) == 10


@pytest.mark.asyncio
async def test_receipt_hint_after_restart_only_with_intent(db_session, monkeypatch):
    """The post-restart receipt TEXT hint — Batch BY moved ownership.

    The hint used to be a payment.py catch-all whose bare filter
    shadowed the private-chat AI entirely; it now runs as the first
    step of group_admin.handle_private_message. These are the same
    assertions as before, aimed at the new owner: WITH a recent
    intent the hint reply (and only the hint) is sent; WITHOUT one
    the hint never fires (the message belongs to the AI pipeline).
    """
    from aiogram.enums import ChatType

    monkeypatch.setattr(pai, "private_rate_limiter", gai.GroupReplyRateLimiter())
    ai = AsyncMock(return_value=(gai.DECISION_NO_REPLY, None))
    monkeypatch.setattr(pai, "ask_private_ai", ai)

    user, _ = await get_or_create_user(db_session, telegram_id=9401)
    await save_payment_intent(db_session, user.id, "MIN_10")
    await db_session.commit()

    def _msg(user_id, first_name, text):
        m = MagicMock(spec=Message)
        m.message_id = 1
        m.from_user = TgUser(id=user_id, is_bot=False, first_name=first_name)
        m.text = text
        m.chat = MagicMock()
        m.chat.id = user_id
        m.chat.type = ChatType.PRIVATE
        m.sender_chat = None
        m.answer = AsyncMock()
        m.reply = AsyncMock()
        m.bot = MagicMock()
        return m

    msg = _msg(9401, "Buyer", "paid already")
    await handle_private_message(
        msg, state=FakeFSMContext(), session=db_session, bot=msg.bot)
    msg.answer.assert_called_once()
    assert "Screenshot" in msg.answer.call_args[0][0]
    # The hint is terminal: the AI pipeline never also answers.
    msg.reply.assert_not_called()
    ai.assert_not_called()

    # No intent -> the hint stays silent (the AI path owns the text;
    # here the mocked AI decides NO_REPLY, so nothing is sent at all).
    msg2 = _msg(9402, "Chatter", "Recap ဘယ်လိုလုပ်ရလဲ")
    await handle_private_message(
        msg2, state=FakeFSMContext(), session=db_session, bot=msg2.bot)
    msg2.answer.assert_not_called()
    msg2.reply.assert_not_called()
    ai.assert_awaited_once()


# ---------------------------------------------------------------------------
# Payment receipt view (kept push-path handler, now router-registered)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pay_receipt_view_sends_photo(db_session):
    settings.admin_ids_raw = "888111"
    admin, _ = await get_or_create_user(db_session, telegram_id=888111)
    admin.language_code = "en"
    buyer, _ = await get_or_create_user(db_session, telegram_id=9501)
    payment = PaymentRequest(
        id="PAY-CLEANUP-1",
        user_id=buyer.id,
        package_code="MIN_10",
        minutes=10,
        amount_mmk=8000,
        status=PaymentStatus.PENDING.value,
        receipt_file_id="FILEID123",
    )
    db_session.add(payment)
    await db_session.flush()

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="Admin")
    cb.data = "admin:pay_receipt:PAY-CLEANUP-1"
    cb.message = MagicMock(spec=Message)
    cb.message.answer_photo = AsyncMock()
    cb.answer = AsyncMock()

    await handle_admin_pay_receipt(cb, session=db_session)
    cb.message.answer_photo.assert_called_once()
    assert cb.message.answer_photo.call_args[1]["photo"] == "FILEID123"


# ---------------------------------------------------------------------------
# Sanitized send-failure copy (no raw exception interpolation)
# ---------------------------------------------------------------------------

def test_send_failure_copy_has_no_error_placeholder():
    for lang in ("my", "en"):
        for key in ("clone_audio_send_failed", "clone_recap_send_failed"):
            assert "{error}" not in t(key, lang)
