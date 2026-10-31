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
from app.handlers.clone import voice_queue_failure_message
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


# --- B1: handle_set_language verified branch prepends welcome block ------

@pytest.mark.asyncio
async def test_set_language_verified_branch_includes_welcome_block(db_session):
    user_id = 730001001
    user, _ = await get_or_create_user(db_session, telegram_id=user_id)
    await db_session.flush()

    msg = MagicMock(spec=Message)
    msg.chat = MagicMock(spec=Chat, id=user_id)
    msg.edit_text = AsyncMock(return_value=msg)

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

    msg.edit_text.assert_awaited_once()
    sent = msg.edit_text.call_args[0][0]
    expected_prefix = f"{t('welcome_title', 'my')}\n\n{t('welcome_desc', 'my')}\n\n"
    assert sent.startswith(expected_prefix)
    # The menu account info follows the welcome block.
    assert "အကောင့်" in sent


# --- B4/B5/B7: main menu keyboard -----------------------------------------

def test_main_menu_recap_downloader_labels_localized():
    my = {b.text for b in _flat(main_menu_kb(is_admin=False, lang="my"))}
    en = {b.text for b in _flat(main_menu_kb(is_admin=False, lang="en"))}
    assert "🎬 Recap ဖန်တီးမည်" in my
    assert "📥 ဒေါင်းလုဒ် လုပ်မည်" in my
    assert "🎬 Movie Recap Maker" in en
    assert "📥 Media Downloader" in en


def test_main_menu_buy_button_after_refer_row():
    for lang, label in (("my", "💎 မိနစ် / Plan ဝယ်မည်"), ("en", "💎 Buy Minutes / Plans")):
        kb = main_menu_kb(is_admin=False, lang=lang)
        flat = _flat(kb)
        buy = [b for b in flat if b.callback_data == "menu:buy_minutes"]
        assert len(buy) == 1
        assert buy[0].text == label
        datas = [b.callback_data for b in flat]
        assert datas.index("menu:buy_minutes") == datas.index("menu:refer_earn") + 1


def test_main_menu_tutorial_button_points_to_index_with_plural_label(monkeypatch):
    monkeypatch.setattr(settings, "tutorial_index_link", "https://t.me/VoiceClonePro/38")
    for lang, label in (
        ("my", "📚 လမ်းညွှန်ဗီဒီယိုများ ကြည့်မည်"),
        ("en", "📚 Watch Tutorial Videos"),
    ):
        flat = _flat(main_menu_kb(is_admin=False, lang=lang))
        tut = [b for b in flat if b.url and "VoiceClonePro" in b.url]
        assert len(tut) == 1
        assert tut[0].url == "https://t.me/VoiceClonePro/38"
        assert tut[0].text == label


def test_main_menu_tutorial_button_honours_custom_index_link(monkeypatch):
    monkeypatch.setattr(settings, "tutorial_index_link", "https://t.me/VoiceClonePro/99")
    flat = _flat(main_menu_kb(is_admin=False, lang="en"))
    tut = [b for b in flat if b.url and "VoiceClonePro" in b.url]
    assert tut[0].url == "https://t.me/VoiceClonePro/99"


# --- BUG: recap_credits 0 vs None ------------------------------------------

@pytest.mark.asyncio
async def test_recap_credits_zero_renders_zero(db_session):
    user_id = 730001002
    user, _ = await get_or_create_user(db_session, telegram_id=user_id)
    user.recap_credits = 0
    await db_session.flush()
    text, _kb = await render_main_menu(user_id, session=db_session)
    assert "<b>0 Credits</b>" in text
    assert "<b>3 Credits</b>" not in text


@pytest.mark.asyncio
async def test_recap_credits_none_falls_back_to_default(db_session):
    stub_user = SimpleNamespace(
        account_category="FREE", language_code="en", recap_credits=None
    )
    stub_balance = SimpleNamespace(total_usable_minutes=1.0, purchased_minutes=0.0)
    with patch(
        "app.handlers.menu.get_or_create_user",
        new=AsyncMock(return_value=(stub_user, False)),
    ), patch(
        "app.handlers.menu.grant_monthly_free_allowance", new=AsyncMock()
    ), patch(
        "app.handlers.menu.get_minute_balance",
        new=AsyncMock(return_value=stub_balance),
    ):
        text, _kb = await render_main_menu(730001003, lang="en", session=db_session)
    assert "<b>3 credits</b>" in text


# --- Batch 4: help guide -----------------------------------------------------

@pytest.mark.parametrize(
    "lang,markers",
    [
        (
            "my",
            [
                "<b>၂။ Movie Recap (Mini App)</b>",
                "<b>၃။ Media Downloader (Mini App)</b>",
                "<b>၄။ လက်ကျန်မိနစ်နှင့် အကောင့်အဆင့်များ</b>",
                "<b>၅။ အခမဲ့မိနစ် ရယူနည်း (Refer & Earn)</b>",
                "<b>၆။ ဘာသာစကား (Language)</b>",
                "• ဖန်တီးထားသော ဖိုင်များကို မှတ်တမ်း (History) မှာ ၇ ရက်အထိ ပြန်ဒေါင်းနိုင်ပါသည်။",
                "• 🚀 <b>Studio ဖွင့်မည်</b> ခလုတ်မှ Voice Tab ကို ဖွင့်ပါ (သို့မဟုတ် ဒီ Chat ထဲမှာပဲ စာသား ပို့လည်း ရပါသည်)။",
                "အပိုင်းတစ်ခုလျှင် 1 Credit",
            ],
        ),
        (
            "en",
            [
                "<b>2. Movie Recap (Mini App)</b>",
                "<b>3. Media Downloader (Mini App)</b>",
                "<b>4. Balances & Tiers</b>",
                "<b>5. Free Minutes (Refer & Earn)</b>",
                "<b>6. Language</b>",
                "• Your creations stay downloadable from History for 7 days.",
                "• 🚀 Open the Voice tab from <b>Launch Studio</b> (or just send your text right here in this chat).",
                "1 credit per part",
            ],
        ),
    ],
)
def test_help_guide_new_sections_and_formatting(lang, markers):
    raw = t("help_settings_guide", lang)
    for m in markers:
        assert m in raw
    # All live placeholders still format without error / leftovers.
    rendered = t(
        "help_settings_guide",
        lang,
        free_min="5",
        free_sec=300,
        char_free="1,000",
        char_purchased="5,000",
        daily_limit=5,
    )
    assert "{free_min}" not in rendered
    assert "{char_free}" not in rendered
    assert "{daily_limit}" not in rendered
    for m in markers:
        assert m in rendered


# --- Batch 5: sample alerts + cvr + queue failure map ------------------------

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


_REASONS = [
    "MODEL_NOT_AUTHORIZED",
    "LEGACY_MODEL_ENGINE_REMOVED",
    "CONCURRENT_JOB_LIMIT_EXCEEDED",
    "DAILY_JOB_LIMIT_EXCEEDED",
    "INSUFFICIENT_BALANCE",
    "TEXT_EXCEEDS_CHARACTER_LIMIT",
    "TEXT_EXCEEDS_INFRASTRUCTURE_LIMIT",
    "AUDIO_FILE_NOT_FOUND",
    "SOME_UNKNOWN_REASON_XYZ",
    None,
    "",
]


@pytest.mark.parametrize("lang", ["my", "en"])
def test_queue_failure_mapping_never_leaks_raw_code(lang):
    for reason in _REASONS:
        msg = voice_queue_failure_message(reason, lang)
        assert msg
        if reason:
            assert reason not in msg
    # Known reasons resolve to the webapp-mirrored Burmese messages.
    assert voice_queue_failure_message("INSUFFICIENT_BALANCE", "my") == "လက်ကျန်မိနစ် မလုံလောက်ပါ။"
    assert voice_queue_failure_message("AUDIO_FILE_NOT_FOUND", "my") == "အသံဖိုင် မတွေ့ရှိပါ။"
    # Unknown reasons get the generic message.
    assert voice_queue_failure_message("WHATEVER", "my") == voice_queue_failure_message(None, "my")


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
