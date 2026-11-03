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
    AdminBroadcastFSM,
    _failure_class,
    handle_admin_broadcast_test,
    handle_admin_pay_receipt,
)
from app.handlers.menu import render_main_menu
from app.handlers.payment import handle_buy_minutes_menu, handle_receipt_hint_after_restart
from app.handlers.start import handle_membership_card
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
# Main menu shape
# ---------------------------------------------------------------------------

def test_main_menu_shape_verified_user():
    kb = main_menu_kb(is_admin=False, lang="my")
    buttons = _flat_buttons(kb)
    # Exactly the 4 core buttons: hero WebApp, Plans, Refer, Language/Help.
    assert len(buttons) == 4
    assert buttons[0].web_app is not None
    assert "tab=" not in (buttons[0].web_app.url or "")
    assert _callbacks(kb) == ["menu:buy_minutes", "menu:refer_earn", "menu:help_settings"]
    # Retired rows: Recap/Downloader deep links, Tutorials, gate toast.
    assert not any(b.web_app and "tab=" in (b.web_app.url or "") for b in buttons)
    assert "gate:verify" not in _callbacks(kb)
    assert "menu:membership" not in _callbacks(kb)
    assert "menu:change_lang" not in _callbacks(kb)


def test_main_menu_shape_membership_pending_and_admin():
    kb = main_menu_kb(is_admin=False, lang="en", membership_pending=True)
    assert "menu:membership" in _callbacks(kb)
    assert len(_flat_buttons(kb)) == 5

    kb_admin = main_menu_kb(is_admin=True, lang="en")
    cbs = _callbacks(kb_admin)
    assert "admin:dashboard" in cbs
    assert "admin:broadcast" in cbs
    assert "menu:membership" not in cbs


@pytest.mark.asyncio
async def test_render_main_menu_membership_button_visibility(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=9101)
    await db_session.flush()
    # Default flags: unverified -> button present.
    _, kb = await render_main_menu(user.id, session=db_session)
    assert "menu:membership" in _callbacks(kb)

    user.channel_verified = True
    user.group_verified = True
    await db_session.flush()
    _, kb2 = await render_main_menu(user.id, session=db_session)
    assert "menu:membership" not in _callbacks(kb2)


# ---------------------------------------------------------------------------
# Membership status card
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_membership_card_shows_only_missing_joins(db_session):
    settings.required_channel_id = "-100111"
    settings.required_group_id = "-100222"
    user, _ = await get_or_create_user(db_session, telegram_id=9102)
    user.language_code = "en"
    await db_session.flush()

    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    member = MagicMock()
    member.status = "member"
    left = MagicMock()
    left.status = "left"

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=9102, is_bot=False, first_name="Joiner")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    cb.bot = MagicMock()
    # Channel joined, group missing.
    cb.bot.get_chat_member = AsyncMock(side_effect=[member, left])

    with patch("app.handlers.start.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_membership_card(cb)

    cb.message.edit_text.assert_called_once()
    text = cb.message.edit_text.call_args[0][0]
    kb = cb.message.edit_text.call_args[1]["reply_markup"]
    urls = [b.url for b in _flat_buttons(kb) if b.url]
    # Only the group join button is offered; channel already joined.
    assert len(urls) == 1
    assert "gate:verify" in _callbacks(kb)
    assert user.channel_verified is True
    assert user.group_verified is False


@pytest.mark.asyncio
async def test_membership_card_verified_user_gets_menu(db_session):
    settings.required_channel_id = "-100111"
    settings.required_group_id = "-100222"
    user, _ = await get_or_create_user(db_session, telegram_id=9103)
    user.language_code = "en"
    await db_session.flush()

    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    member = MagicMock()
    member.status = "member"

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=9103, is_bot=False, first_name="Member")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    cb.bot = MagicMock()
    cb.bot.get_chat_member = AsyncMock(return_value=member)

    with patch("app.handlers.start.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_membership_card(cb)

    # Verified toast shown and the re-rendered menu drops the button.
    cb.answer.assert_called_once_with(t("membership_verified", "en"))
    kb = cb.message.edit_text.call_args[1]["reply_markup"]
    assert "menu:membership" not in _callbacks(kb)


# ---------------------------------------------------------------------------
# Language screen
# ---------------------------------------------------------------------------

def test_language_selection_kb_back_and_active_marker():
    kb = language_selection_kb(active_lang="my", show_back=True, lang="my")
    buttons = _flat_buttons(kb)
    assert buttons[0].text.startswith("✅")
    assert not buttons[1].text.startswith("✅")
    assert _callbacks(kb) == ["set_lang:my", "set_lang:en", "menu:home"]

    kb_plain = language_selection_kb()
    assert _callbacks(kb_plain) == ["set_lang:my", "set_lang:en"]
    assert not any(b.text.startswith("✅") for b in _flat_buttons(kb_plain))


# ---------------------------------------------------------------------------
# Broadcast: 0-audience hiding + test-send
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


@pytest.mark.asyncio
async def test_broadcast_test_send_only_to_admin_and_keeps_state(db_session):
    settings.admin_ids_raw = "888111"
    admin, _ = await get_or_create_user(db_session, telegram_id=888111)
    admin.language_code = "en"
    await db_session.flush()

    state = FakeFSMContext(
        initial_state=AdminBroadcastFSM.confirming,
        initial_data={"token": "tok123", "broadcast_text": "Hello everyone", "lang": "en"},
    )
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="Admin")
    cb.data = "admin:broadcast_test:tok123"
    cb.answer = AsyncMock()
    cb.bot = MagicMock()
    cb.bot.send_message = AsyncMock()

    await handle_admin_broadcast_test(cb, state, session=db_session)

    cb.bot.send_message.assert_called_once()
    kwargs = cb.bot.send_message.call_args[1]
    assert kwargs["chat_id"] == 888111
    assert kwargs["text"] == "Hello everyone"
    # State untouched: the admin can still confirm or cancel.
    assert state.current_state == AdminBroadcastFSM.confirming
    assert state.data.get("token") == "tok123"
    cb.answer.assert_called_once_with(t("admin_alert_broadcast_test_sent", "en"))

    # Wrong nonce: nothing is sent, expiry alert instead.
    cb2 = MagicMock(spec=CallbackQuery)
    cb2.from_user = TgUser(id=888111, is_bot=False, first_name="Admin")
    cb2.data = "admin:broadcast_test:WRONG"
    cb2.answer = AsyncMock()
    cb2.bot = MagicMock()
    cb2.bot.send_message = AsyncMock()
    await handle_admin_broadcast_test(cb2, state, session=db_session)
    cb2.bot.send_message.assert_not_called()
    assert cb2.answer.call_args[1].get("show_alert") is True


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
        # All 8 catalogue buttons remain available as top-ups.
        kb = cb_vip.message.edit_text.call_args[1]["reply_markup"]
        buy_cbs = [c for c in _callbacks(kb) if c.startswith("buy_pkg:")]
        assert len(buy_cbs) == 8


@pytest.mark.asyncio
async def test_receipt_hint_after_restart_only_with_intent(db_session):
    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    user, _ = await get_or_create_user(db_session, telegram_id=9401)
    await save_payment_intent(db_session, user.id, "MIN_10")
    await db_session.commit()

    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=9401, is_bot=False, first_name="Buyer")
    msg.text = "paid already"
    msg.chat = MagicMock()
    msg.chat.type = "private"
    msg.answer = AsyncMock()

    with patch("app.handlers.payment.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_receipt_hint_after_restart(msg)
    msg.answer.assert_called_once()
    assert "Screenshot" in msg.answer.call_args[0][0]

    # No intent -> silence (ordinary text is never answered).
    msg2 = MagicMock(spec=Message)
    msg2.from_user = TgUser(id=9402, is_bot=False, first_name="Chatter")
    msg2.text = "hello"
    msg2.chat = MagicMock()
    msg2.chat.type = "private"
    msg2.answer = AsyncMock()
    with patch("app.handlers.payment.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_receipt_hint_after_restart(msg2)
    msg2.answer.assert_not_called()


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
