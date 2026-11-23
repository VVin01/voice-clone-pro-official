"""Bot-side toast/start fixes 2026-10-31 (Batches 3-5 bot part).

Pins: welcome composition on the set-language path, shortened
welcome_desc, localized Recap/Downloader buttons, the new Buy row,
the tutorial-index button, recap_credits=0 rendering, the refreshed
/help guide, localized sample alerts, the queue-failure reason map,
the cvr limit text, maintenance language resolution and banned /start.
"""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User as TgUser

from app.config import settings
from app.handlers.menu import render_main_menu
from app.handlers.start import handle_set_language, handle_start
from app.models.enums import UserStatus
from app.services.i18n import t
from app.services.ledger import get_or_create_user
from app.ui.keyboards import main_menu_kb


def _flat(kb):
    return [btn for row in kb.inline_keyboard for btn in row]


def _session_ctx(db_session):
    @asynccontextmanager
    async def _ctx():
        yield db_session

    return lambda: _ctx()


# --- B2: welcome_desc shortened -------------------------------------------

def test_welcome_desc_shortened_both_locales():
    assert t("welcome_desc", "my") == "မြန်မာ့ All-in-One AI Studio မှ လှိုက်လှဲစွာ ကြိုဆိုပါသည်။"
    assert t("welcome_desc", "en") == "Welcome to Myanmar's all-in-one AI studio."


# --- B1: handle_set_language verified branch sends compact welcome (Round 13 Part K) ------

@pytest.mark.asyncio
async def test_set_language_verified_branch_includes_welcome_block(db_session):
    user_id = 730001001
    user, _ = await get_or_create_user(db_session, telegram_id=user_id)
    await db_session.flush()

    msg = MagicMock(spec=Message)
    msg.chat = MagicMock(spec=Chat, id=user_id)
    msg.delete = AsyncMock()
    msg.answer = AsyncMock(return_value=msg)

    cb = MagicMock(spec=CallbackQuery)
    cb.message = msg
    cb.from_user = TgUser(id=user_id, is_bot=False, first_name="NewUser")
    cb.bot = MagicMock()
    cb.data = "set_lang:my"
    cb.answer = AsyncMock()

    with patch("app.handlers.start.AsyncSessionLocal", side_effect=_session_ctx(db_session)), patch(
        "app.handlers.start.verify_user_membership", new=AsyncMock(return_value=(True, "ok"))
    ):
        await handle_set_language(cb)

    msg.delete.assert_awaited_once()
    msg.answer.assert_awaited_once()
    sent = msg.answer.call_args[0][0]
    assert "Voice Clone Pro မှ ကြိုဆိုပါတယ်" in sent


# --- Batch 5: sample alerts + cvr + tts queue copy (Round 13 Part K retired bot clone/help/buy menu) ---

def test_sample_alert_keys_both_locales():
    assert t("sample_invalid_request", "my") == "နမူနာ တောင်းဆိုမှု မမှန်ပါ"
    assert t("sample_invalid_request", "en") == "Invalid sample request"
    assert t("sample_model_not_found", "my") == "အသံ မော်ဒယ် မတွေ့ပါ"
    assert t("sample_model_not_found", "en") == "Model not found"
    assert t("sample_send_failed", "my") == "⚠️ နမူနာအသံ ပို့မရပါ — ခဏနေ ပြန်ကြိုးစားပါ"
    assert t("sample_send_failed", "en") == "⚠️ Couldn't send the audio sample — please try again shortly"


def test_cvr_limit_reached_new_text():
    assert t("cvr_limit_reached", "my") == "❌ Custom Voice တောင်းဆိုမှု ကန့်သတ်ချက် ပြည့်သွားပါပြီ — နောက်မှ ပြန်ကြိုးစားပါ"
    assert t("cvr_limit_reached", "en") == "❌ Custom Voice request limit reached — please try again later"


def test_tts_queue_failed_key_has_no_reason_placeholder():
    for lang in ("my", "en"):
        assert "{reason}" not in t("tts_queue_failed", lang)


# --- B6: maintenance uses the stored user language ---------------------------

@pytest.mark.asyncio
async def test_maintenance_notice_uses_stored_language(db_session, monkeypatch):
    user_id = 730001004
    user, _ = await get_or_create_user(db_session, telegram_id=user_id)
    user.language_code = "en"
    await db_session.flush()
    monkeypatch.setattr(settings, "maintenance_mode", True)

    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=user_id, is_bot=False, first_name="MaintUser")
    msg.answer = AsyncMock()
    cmd = MagicMock()
    cmd.args = None

    with patch("app.handlers.start.AsyncSessionLocal", side_effect=_session_ctx(db_session)):
        await handle_start(msg, cmd)

    msg.answer.assert_awaited_once()
    sent = msg.answer.call_args[0][0]
    assert t("maintenance_title", "en") in sent
    assert t("maintenance_title", "my") not in sent


# --- B8(b): banned user gets suspension notice, no menu ----------------------

@pytest.mark.asyncio
async def test_banned_user_start_gets_suspension_notice_no_menu(db_session):
    user_id = 730001005
    user, _ = await get_or_create_user(db_session, telegram_id=user_id)
    user.language_code = "en"
    user.status = UserStatus.BLOCKED.value
    await db_session.flush()

    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=user_id, is_bot=False, first_name="BannedUser")
    msg.answer = AsyncMock()
    cmd = MagicMock()
    cmd.args = None

    with patch("app.handlers.start.AsyncSessionLocal", side_effect=_session_ctx(db_session)):
        await handle_start(msg, cmd)

    msg.answer.assert_awaited_once()
    sent = msg.answer.call_args[0][0]
    assert sent == t("user_account_suspended", "en")
    # No menu keyboard is attached.
    assert msg.answer.call_args[1].get("reply_markup") is None
