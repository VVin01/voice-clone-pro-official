"""Batch BY (2026-11-07): private-AI routing fix + settings hardening.

Item 1 (MAIN) — Batch BX's private-chat AI handler never fired in
production: payment.py's no-state private-text catch-all
(handle_receipt_hint_after_restart) sat on payment_router, included
BEFORE group_admin_router, and consumed every plain private text even
when it returned silently. BX's tests called the handler directly, so
the suite stayed green while the feature was dead live (eyes-on FAIL
1: greetings/questions/payment texts all got silence, zero private_ai
log lines). The fix makes group_admin.handle_private_message the
single terminal owner of plain private text and folds the
payment-intent hint in as its first step (same helper, same reply).

The routing tests below therefore feed synthetic updates through the
REAL dispatcher assembled by app.handlers.setup_handlers — the exact
router order production runs — not direct handler calls.

Item 2 — Admin Settings edit-clobber protection in app.js (static
pins): loadAdminSettings() must never repaint a field the admin has
edited since the last load; the dirty guard clears only after a
successful saveAdminSettings().
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, MessageEntity, Update
from aiogram.types import Message as TgMessage
from aiogram.types import User as TgUser

from app.config import settings
from app.handlers import group_admin as ga_mod
from app.handlers import setup_handlers
from app.services import group_ai_admin as gai
from app.services import private_ai as pai
from app.services.i18n import load_locales, t
from app.services.ledger import get_or_create_user
from app.services.payments import save_payment_intent

ROOT = Path(__file__).resolve().parents[2]

load_locales()


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ===========================================================================
# Item 1 — dispatcher-level routing tests (real router assembly order)
# ===========================================================================

QUESTION = "Recap ဘယ်လိုလုပ်ရလဲ"  # service question: no fast path, not sensitive


def _mock_session_maker(session):
    sm = MagicMock()
    sm.return_value.__aenter__ = AsyncMock(return_value=session)
    sm.return_value.__aexit__ = AsyncMock(return_value=None)
    return sm


def _make_message_update(user_id: int, text: str, update_id: int, *, command: bool = False) -> Update:
    chat = Chat(id=user_id, type="private")
    user = TgUser(id=user_id, is_bot=False, first_name="RouterTest")
    entities = None
    if command:
        entities = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
    msg = TgMessage(
        message_id=update_id,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text=text,
        entities=entities,
    )
    return Update(update_id=update_id, message=msg)


def _sent_texts(bot) -> list[str]:
    return [
        c[0][0].text
        for c in bot.call_args_list
        if c[0] and type(c[0][0]).__name__ == "SendMessage"
    ]


@pytest.fixture()
def routed(db_session, monkeypatch):
    """A real dispatcher (production router order) over the test DB.

    Returns (dp, bot, ai_mock, intent_spy): the AI call is mocked at
    the private_ai service boundary, and the payment-intent lookup is
    spied at the group_admin module boundary — the folded hint must
    consult it from inside handle_private_message.
    """
    monkeypatch.setattr(settings, "admin_ids_raw", "999000")
    sm = _mock_session_maker(db_session)
    monkeypatch.setattr("app.handlers.group_admin.AsyncSessionLocal", sm)
    monkeypatch.setattr("app.handlers.start.AsyncSessionLocal", sm)
    monkeypatch.setattr("app.handlers.payment.AsyncSessionLocal", sm)
    monkeypatch.setattr(pai, "private_rate_limiter", gai.GroupReplyRateLimiter())
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "AI_TEST_REPLY"))
    monkeypatch.setattr(pai, "ask_private_ai", ai)
    intent_spy = AsyncMock(side_effect=ga_mod.get_recent_payment_intent)
    monkeypatch.setattr(ga_mod, "get_recent_payment_intent", intent_spy)

    dp = Dispatcher(storage=MemoryStorage())
    setup_handlers(dp, session_factory=sm)
    bot = AsyncMock(spec=Bot)
    bot.id = 999888
    return dp, bot, ai, intent_spy


@pytest.mark.asyncio
async def test_routing_plain_text_reaches_private_ai(routed, caplog):
    """(a) No state + no intent + non-admin → the private-AI path runs."""
    dp, bot, ai, intent_spy = routed
    with caplog.at_level(logging.INFO, logger="app.handlers.group_admin"):
        await dp.feed_update(bot, _make_message_update(7701, QUESTION, 1))
    ai.assert_awaited_once()
    intent_spy.assert_awaited_once()  # the folded hint check ran first
    assert "AI_TEST_REPLY" in _sent_texts(bot)
    assert "private_ai: decision=reply" in caplog.text


@pytest.mark.asyncio
async def test_routing_payment_intent_gets_hint_only(routed, db_session):
    """(b) No state + recent intent → hint reply, AI never invoked."""
    dp, bot, ai, intent_spy = routed
    user, _ = await get_or_create_user(db_session, telegram_id=7702)
    await save_payment_intent(db_session, user.id, "MIN_10")
    await db_session.commit()

    await dp.feed_update(bot, _make_message_update(7702, "paid already", 2))
    expected = t("pay_receipt_wrong_content", user.language_code or "my")
    assert _sent_texts(bot) == [expected]
    ai.assert_not_called()
    intent_spy.assert_awaited_once()


class _ByFlow(StatesGroup):
    waiting = State()


@pytest.mark.asyncio
async def test_routing_fsm_state_blocks_hint_and_ai(routed, db_session):
    """(c) FSM state set → neither the hint nor the AI fires.

    The user even HAS a payment intent: the state check precedes the
    intent check, so the state owners keep full ownership of the text.
    """
    dp, bot, ai, intent_spy = routed
    user, _ = await get_or_create_user(db_session, telegram_id=7703)
    await save_payment_intent(db_session, user.id, "MIN_10")
    await db_session.commit()
    key = StorageKey(bot_id=bot.id, chat_id=7703, user_id=7703)
    await dp.storage.set_state(key, _ByFlow.waiting)

    await dp.feed_update(bot, _make_message_update(7703, QUESTION, 3))
    ai.assert_not_called()
    intent_spy.assert_not_called()
    assert _sent_texts(bot) == []


@pytest.mark.asyncio
async def test_routing_start_command_stays_with_start_handler(routed):
    """(d) /start → owned by its command handler, not the AI path."""
    dp, bot, ai, intent_spy = routed
    await dp.feed_update(bot, _make_message_update(7704, "/start", 4, command=True))
    ai.assert_not_called()
    intent_spy.assert_not_called()
    # A brand-new user is answered by /start with the language pick.
    assert t("welcome_choose_lang", "my") in _sent_texts(bot)


# ===========================================================================
# Item 1 — static pins: single terminal owner + step ordering
# ===========================================================================

def test_payment_router_has_no_plain_text_catch_all():
    src = _read("app/handlers/payment.py")
    assert "async def handle_receipt_hint_after_restart" not in src
    # The MEDIA restart twin is untouched and still registered.
    assert "async def handle_receipt_after_restart" in src


def test_private_handler_step_ordering():
    src = _read("app/handlers/group_admin.py")
    body = src.split("async def handle_private_message", 1)[1].split(
        "@group_admin_router.callback_query", 1)[0]
    i_state = body.index("state.get_state()")
    i_intent = body.index("get_recent_payment_intent")
    i_prefilter = body.index("prefilter_private_reason")
    i_sensitive = body.index("detect_sensitive_topic")
    assert i_state < i_intent < i_prefilter < i_sensitive
    # Same helper + same reply text as the deleted payment handler.
    assert 'pay_receipt_wrong_content' in body


# ===========================================================================
# Item 2 — Admin Settings dirty guard (static-JS pins)
# ===========================================================================

ADMIN_JS = _read("app/webapp/static/js/admin.js")


def _load_body() -> str:
    return ADMIN_JS.split("async function loadAdminSettings", 1)[1].split(
        "async function saveGeminiModelSettings", 1)[0]


def _save_body() -> str:
    return ADMIN_JS.split("async function saveAdminSettings", 1)[1].split(
        "\n}\n", 1)[0]


def test_dirty_guard_is_generic_over_the_settings_pane():
    # Tracking binds by delegation on the settings pane (every painted
    # field lives inside it) — not a per-field list that can drift.
    assert "const adminSettingsDirty = new Set()" in ADMIN_JS
    assert 'document.getElementById("adm-screen-settings")' in ADMIN_JS
    assert 'pane.addEventListener("input", mark)' in ADMIN_JS
    assert 'pane.addEventListener("change", mark)' in ADMIN_JS
    assert "_bindAdminSettingsDirtyTracking()" in _load_body()


def test_load_paints_char_limits_through_the_guard():
    body = _load_body()
    assert "_admPaintValue(elCharLimitFree, s.character_limit_free)" in body
    assert "_admPaintValue(elCharLimitPurchased, s.character_limit_purchased)" in body
    # No unguarded direct paint of the char-limit fields remains.
    assert "elCharLimitPurchased.value =" not in body
    assert "elCharLimitFree.value =" not in body


def test_dirty_guard_clears_only_after_successful_settings_save():
    body = _save_body()
    assert "adminSettingsDirty.clear()" in body
    # The clear sits AFTER the success gate: a failed save keeps the
    # admin's edits protected from the next reload.
    assert body.index("if (!res.ok) throw") < body.index("adminSettingsDirty.clear()")
    # The Gemini model-chain save must respect the guard (it repaints
    # via loadAdminSettings) but never clear it.
    gemini_body = ADMIN_JS.split("async function saveGeminiModelSettings", 1)[1].split(
        "async function saveAdminSettings", 1)[0]
    assert "adminSettingsDirty.clear()" not in gemini_body
    # And the model selects are guarded too.
    assert "adminSettingsDirty.has(selectId)" in ADMIN_JS
