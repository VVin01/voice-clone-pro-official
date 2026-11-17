"""Comprehensive unit tests for Group AI — Moderation Ladder + Voice Understanding + Admin Console (2026-11-16).

Covers:
- Part 1: Moderation ladder (Strike 1 warn, Strike 2 mute 1h, Strike 3 mute 24h + Admin DM with Ban button)
- Rolling 7-day strike window and DB storage rule (group message text NEVER stored in DB)
- Exemptions (admins, bots, whitelist, sensitive topics)
- Admin-only Ban callback execution
- Part 2: Voice message understanding (Gemini ASR transcription, duration/size caps, daily cap, silence on failure)
- Part 3: Mini App Admin Tab Group AI Console (knowledge sheet save/reset, 8k cap, budgets, recent actions, guardrail precedence)
- Cache bust version 20261117_r160 verification
"""

import io
import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.enums import ChatType
from aiogram.types import Message, User as TgUser, Voice
from sqlalchemy import select

from app.config import settings
from app.handlers.group_admin import (
    handle_group_message,
    handle_moderation_ban_callback,
)
from app.models.schema import (
    AuditLog,
    BotSetting,
    GroupAiModerationStrike,
    User,
)
from app.services import group_ai_admin as gai

GROUP_ID = gai.COMMUNITY_GROUP_ID  # -1003795677289
ADMIN_ID = 424242
ADMIN2_ID = 434343
REGULAR_USER_ID = 888111

_sent_ids = itertools.count(9500)


def _fake_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=lambda *a, **k: SimpleNamespace(message_id=next(_sent_ids))
    )
    bot.edit_message_text = AsyncMock()
    bot.edit_message_reply_markup = AsyncMock()
    bot.restrict_chat_member = AsyncMock()
    bot.ban_chat_member = AsyncMock()
    bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="voice/file_1.ogg"))
    bot.download_file = AsyncMock(return_value=io.BytesIO(b"fake_voice_bytes"))
    return bot


def _fake_message(text=None, *, user_id=REGULAR_USER_ID, message_id=20, chat_id=GROUP_ID, voice=None):
    msg = MagicMock(spec=Message)
    msg.message_id = message_id
    msg.text = text
    msg.voice = voice
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.chat.type = ChatType.SUPERGROUP
    msg.from_user = TgUser(id=user_id, is_bot=False, first_name="User", username="user888")
    msg.sender_chat = None
    msg.reply_to_message = None
    msg.reply = AsyncMock()
    msg.answer = AsyncMock()
    msg.bot = _fake_bot()
    return msg


def _fake_voice_message(duration=10, file_size=1024, *, user_id=REGULAR_USER_ID, message_id=21):
    voice = MagicMock(spec=Voice)
    voice.file_id = "voice_file_123"
    voice.duration = duration
    voice.file_size = file_size
    voice.mime_type = "audio/ogg"
    return _fake_message(text=None, user_id=user_id, message_id=message_id, voice=voice)


def _fake_state():
    state = MagicMock()
    state.get_state = AsyncMock(return_value=None)
    return state


async def _enable_group_ai(db_session):
    db_session.add(BotSetting(key=gai.KILL_SWITCH_KEY, value="true"))
    await db_session.commit()


@pytest.fixture(autouse=True)
def _fresh_rate_limiter(monkeypatch):
    monkeypatch.setattr(gai, "rate_limiter", gai.GroupReplyRateLimiter())
    monkeypatch.setattr(gai, "context_buffer", gai.GroupContextBuffer())


# ===========================================================================
# Part 1: Moderation Ladder Tests
# ===========================================================================


def test_detect_moderation_violation_deterministic():
    """Deterministic rules catch selling links, telegram links outside community, sales terms."""
    # Selling link
    cat, conf = gai.detect_moderation_violation("မင်္ဂလာပါ https://t.me/external_bot_channel")
    assert cat == "selling"
    assert conf >= 0.9

    # Telegram link inside community group is NOT flagged
    cat, conf = gai.detect_moderation_violation("ဝင်ရောက်ရန် https://t.me/voiceclonepro_chat")
    assert cat is None

    # Burmese sales keyword
    cat, conf = gai.detect_moderation_violation("အကောင့်ရောင်းမည် စျေးသက်သာ")
    assert cat == "selling"
    assert conf >= 0.9

    # Normal harmless text
    cat, conf = gai.detect_moderation_violation("အသံသွင်းတာ ဘယ်လိုလုပ်ရမလဲ ခင်ဗျာ")
    assert cat is None
    assert conf == 0.0


def test_parse_ai_decision_moderation():
    """DecisionResult unpacks into (decision, reply) while exposing moderation dict."""
    raw = (
        "REPLY: မင်္ဂလာပါ\n"
        "MODERATION: category=selling confidence=0.95"
    )
    res = gai.parse_ai_decision(raw)
    dec, reply = res
    assert dec == gai.DECISION_REPLY
    assert reply == "မင်္ဂလာပါ"
    assert res.moderation["category"] == "selling"
    assert res.moderation["confidence"] == 0.95


@pytest.mark.asyncio
async def test_strike_ladder_strike_1(db_session, monkeypatch):
    """Strike 1: public warning reply, message left in place, strike 1 row recorded."""
    await _enable_group_ai(db_session)
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))

    msg = _fake_message("အကောင့်ရောင်းမည် စျေးသက်သာ ဆက်သွယ်ရန်", user_id=REGULAR_USER_ID, message_id=101)
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)

    # Replied with Strike 1 warning text (polite, zero emoji)
    msg.reply.assert_called_once_with(gai.WARNING_TEXT_STRIKE_1)
    assert "⚠️" not in gai.WARNING_TEXT_STRIKE_1  # zero emoji

    # Bot restrict_chat_member was NOT called on strike 1
    msg.bot.restrict_chat_member.assert_not_called()

    # DB row recorded
    strikes = await gai.get_user_strikes_in_window(db_session, REGULAR_USER_ID)
    assert len(strikes) == 1
    assert strikes[0].action == "warn"
    assert strikes[0].category == "selling"
    assert strikes[0].message_id == 101

    # CRITICAL: Verify group message text is NOT stored in strike row
    stmt = select(GroupAiModerationStrike).where(GroupAiModerationStrike.user_id == REGULAR_USER_ID)
    row = (await db_session.execute(stmt)).scalar_one()
    assert not hasattr(row, "text") and not hasattr(row, "message_text")


@pytest.mark.asyncio
async def test_strike_ladder_strike_2(db_session, monkeypatch):
    """Strike 2: public warning reply + 1h mute via restrict_chat_member."""
    await _enable_group_ai(db_session)
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))

    # Pre-seed Strike 1
    await gai.record_moderation_strike(
        db_session,
        user_id=REGULAR_USER_ID,
        category="selling",
        message_id=100,
        chat_id=GROUP_ID,
        action="warn",
    )

    msg = _fake_message("အကောင့်ရောင်းမည် ဆက်သွယ်ပါ", user_id=REGULAR_USER_ID, message_id=102)
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)

    # Replied with Strike 2 warning
    msg.reply.assert_called_once_with(gai.WARNING_TEXT_STRIKE_2)

    # Muted for 1 hour (can_send_messages=False)
    msg.bot.restrict_chat_member.assert_called_once()
    _, kwargs = msg.bot.restrict_chat_member.call_args
    assert kwargs["chat_id"] == GROUP_ID
    assert kwargs["user_id"] == REGULAR_USER_ID
    permissions = kwargs["permissions"]
    assert permissions.can_send_messages is False
    assert kwargs["until_date"] is not None

    # Strike 2 recorded
    strikes = await gai.get_user_strikes_in_window(db_session, REGULAR_USER_ID)
    assert len(strikes) == 2
    assert strikes[-1].action == "mute_1h"


@pytest.mark.asyncio
async def test_strike_ladder_strike_3(db_session, monkeypatch):
    """Strike 3: 24h mute + Admin escalation DM with Ban button (AI NEVER bans directly)."""
    await _enable_group_ai(db_session)
    monkeypatch.setattr(settings, "admin_ids_raw", f"{ADMIN_ID},{ADMIN2_ID}")

    # Pre-seed Strike 1 and Strike 2
    await gai.record_moderation_strike(
        db_session,
        user_id=REGULAR_USER_ID,
        category="selling",
        message_id=100,
        chat_id=GROUP_ID,
        action="warn",
    )
    await gai.record_moderation_strike(
        db_session,
        user_id=REGULAR_USER_ID,
        category="selling",
        message_id=101,
        chat_id=GROUP_ID,
        action="mute_1h",
    )

    msg = _fake_message("အကောင့်ရောင်းမည် တတိယအကြိမ်", user_id=REGULAR_USER_ID, message_id=103)
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)

    # Replied with Strike 3 warning
    msg.reply.assert_called_once_with(gai.WARNING_TEXT_STRIKE_3)

    # Muted 24h
    msg.bot.restrict_chat_member.assert_called_once()

    # The AI did NOT ban the member directly!
    msg.bot.ban_chat_member.assert_not_called()

    # Admin DM sent to both admins with Ban button
    assert msg.bot.send_message.call_count == 2
    dm_calls = msg.bot.send_message.call_args_list
    admin_targets = {call.args[0] if call.args else call.kwargs.get("chat_id") for call in dm_calls}
    assert admin_targets == {ADMIN_ID, ADMIN2_ID}

    # Verify inline keyboard has Ban button
    kb = dm_calls[0].kwargs.get("reply_markup")
    ban_btn = kb.inline_keyboard[0][0]
    assert "Ban" in ban_btn.text
    assert ban_btn.callback_data.startswith("gai_ban:")
    assert f":{GROUP_ID}:{REGULAR_USER_ID}" in ban_btn.callback_data


@pytest.mark.asyncio
async def test_rolling_7_day_strike_window(db_session, monkeypatch):
    """Strikes older than 7 days do NOT count towards the ladder."""
    await _enable_group_ai(db_session)
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))

    # Seed strike 8 days ago
    old_time = datetime.now(timezone.utc) - timedelta(days=8)
    old_strike = GroupAiModerationStrike(
        user_id=REGULAR_USER_ID,
        category="selling",
        message_id=99,
        chat_id=GROUP_ID,
        action="warn",
        created_at=old_time,
    )
    db_session.add(old_strike)
    await db_session.commit()

    # A new violation should trigger Strike 1 again, NOT Strike 2
    msg = _fake_message("အကောင့်ရောင်းမည် စျေးသက်သာ", user_id=REGULAR_USER_ID, message_id=104)
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)

    msg.reply.assert_called_once_with(gai.WARNING_TEXT_STRIKE_1)
    msg.bot.restrict_chat_member.assert_not_called()


@pytest.mark.asyncio
async def test_moderation_confidence_below_threshold_ignored(db_session, monkeypatch):
    """Verdicts below threshold (default 0.8) are completely ignored without reply or strike."""
    await _enable_group_ai(db_session)
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))

    # Mock ask_group_admin_ai with confidence 0.65 (< 0.8)
    res = gai.DecisionResult(gai.DECISION_NO_REPLY, None, {"category": "selling", "confidence": 0.65})
    monkeypatch.setattr(gai, "ask_group_admin_ai", AsyncMock(return_value=res))

    msg = _fake_message("ပုံမှန် စကားပြော စာသား", user_id=REGULAR_USER_ID, message_id=105)
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)

    msg.reply.assert_not_called()
    msg.bot.restrict_chat_member.assert_not_called()
    strikes = await gai.get_user_strikes_in_window(db_session, REGULAR_USER_ID)
    assert len(strikes) == 0


@pytest.mark.asyncio
async def test_exemptions_admins_and_whitelist_never_moderated(db_session, monkeypatch):
    """Admins and whitelisted users are never subjected to moderation."""
    await _enable_group_ai(db_session)
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))

    # 1. Admin posting selling phrase
    admin_msg = _fake_message("အကောင့်ရောင်းမည် စျေးသက်သာ", user_id=ADMIN_ID, message_id=106)
    await handle_group_message(admin_msg, state=_fake_state(), session=db_session, bot=admin_msg.bot)
    admin_msg.reply.assert_not_called()
    strikes = await gai.get_user_strikes_in_window(db_session, ADMIN_ID)
    assert len(strikes) == 0

    # 2. Whitelisted user
    whitelisted_user_id = 999222
    monkeypatch.setattr(settings, "group_ai_moderation_whitelist", f"12345\n{whitelisted_user_id}\n67890")
    wl_msg = _fake_message("အကောင့်ရောင်းမည် စျေးသက်သာ", user_id=whitelisted_user_id, message_id=107)
    await handle_group_message(wl_msg, state=_fake_state(), session=db_session, bot=wl_msg.bot)
    wl_msg.reply.assert_not_called()
    strikes = await gai.get_user_strikes_in_window(db_session, whitelisted_user_id)
    assert len(strikes) == 0


@pytest.mark.asyncio
async def test_sensitive_topics_never_enter_moderation(db_session, monkeypatch):
    """Payment/refund questions ALWAYS defer to human admins and NEVER trigger moderation ladder."""
    await _enable_group_ai(db_session)
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))

    # Payment inquiry with sales keyword
    msg = _fake_message("ငွေသွင်းပြီးပြီ အကောင့်ရောင်းမည် မဟုတ်ပါ စစ်ပေးပါ", user_id=REGULAR_USER_ID, message_id=108)
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)

    # Follows standard sensitive deferral
    msg.reply.assert_called_once_with(gai.DEFERRAL_TEXT_SENSITIVE)
    # No strike recorded!
    strikes = await gai.get_user_strikes_in_window(db_session, REGULAR_USER_ID)
    assert len(strikes) == 0


@pytest.mark.asyncio
async def test_moderation_ban_callback(db_session, monkeypatch):
    """Admin tap on Ban button executes ban_chat_member and updates strike row."""
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))

    strike = await gai.record_moderation_strike(
        db_session,
        user_id=REGULAR_USER_ID,
        category="selling",
        message_id=109,
        chat_id=GROUP_ID,
        action="mute_24h",
    )

    # Non-admin tap rejected
    non_admin_cb = MagicMock()
    non_admin_cb.data = f"gai_ban:{strike.id}:{GROUP_ID}:{REGULAR_USER_ID}"
    non_admin_cb.from_user = TgUser(id=999999, is_bot=False, first_name="Intruder")
    non_admin_cb.answer = AsyncMock()
    non_admin_cb.bot = _fake_bot()
    await handle_moderation_ban_callback(non_admin_cb, session=db_session)
    non_admin_cb.answer.assert_called_once()
    assert non_admin_cb.answer.call_args[1]["show_alert"] is True
    non_admin_cb.bot.ban_chat_member.assert_not_called()

    # Admin tap executes ban
    admin_cb = MagicMock()
    admin_cb.data = f"gai_ban:{strike.id}:{GROUP_ID}:{REGULAR_USER_ID}"
    admin_cb.from_user = TgUser(id=ADMIN_ID, is_bot=False, first_name="SuperAdmin")
    admin_cb.answer = AsyncMock()
    admin_cb.message = MagicMock()
    admin_cb.message.edit_text = AsyncMock()
    admin_cb.message.text = "Offending message DM content"
    admin_cb.bot = _fake_bot()

    await handle_moderation_ban_callback(admin_cb, session=db_session)
    admin_cb.bot.ban_chat_member.assert_called_once_with(chat_id=GROUP_ID, user_id=REGULAR_USER_ID)
    admin_cb.message.edit_text.assert_called_once()

    # Verify DB updated
    await db_session.refresh(strike)
    assert strike.banned_by == ADMIN_ID
    assert strike.banned_at is not None


# ===========================================================================
# Part 2: Voice Message Understanding Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_voice_message_flow(db_session, monkeypatch):
    """Voice message is downloaded, transcribed via Gemini pool, and flows into standard pipeline."""
    await _enable_group_ai(db_session)
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))

    # Mock Gemini ASR
    monkeypatch.setattr(gai, "transcribe_voice_gemini", AsyncMock(return_value="အသံပြောင်းတာ ဘယ်လိုလုပ်ရတာလဲ"))

    # Mock ask_group_admin_ai
    mock_ai = AsyncMock(return_value=gai.DecisionResult(gai.DECISION_REPLY, "မင်္ဂလာပါ Voice Clone Pro မှာ ပြုလုပ်နိုင်ပါတယ်"))
    monkeypatch.setattr(gai, "ask_group_admin_ai", mock_ai)

    msg = _fake_voice_message(duration=15, file_size=2048)
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)

    # Bot downloaded voice
    msg.bot.get_file.assert_called_once_with("voice_file_123")
    msg.bot.download_file.assert_called_once()

    # ask_group_admin_ai called with transcript text
    mock_ai.assert_called_once()
    assert mock_ai.call_args.kwargs["text"] == "အသံပြောင်းတာ ဘယ်လိုလုပ်ရတာလဲ"

    # Replied in text
    msg.reply.assert_called_once_with("မင်္ဂလာပါ Voice Clone Pro မှာ ပြုလုပ်နိုင်ပါတယ်")
    assert gai.rate_limiter.voice_daily_used() == 1


@pytest.mark.asyncio
async def test_voice_caps_enforced(db_session, monkeypatch):
    """Voice > 120s or > 5MB is silently ignored; daily transcription cap enforced."""
    await _enable_group_ai(db_session)

    # 1. Voice duration > 120s
    long_msg = _fake_voice_message(duration=121, file_size=1024)
    await handle_group_message(long_msg, state=_fake_state(), session=db_session, bot=long_msg.bot)
    long_msg.bot.get_file.assert_not_called()
    long_msg.reply.assert_not_called()

    # 2. Voice file size > 5MB
    large_msg = _fake_voice_message(duration=30, file_size=6 * 1024 * 1024)
    await handle_group_message(large_msg, state=_fake_state(), session=db_session, bot=large_msg.bot)
    large_msg.bot.get_file.assert_not_called()

    # 3. Daily voice transcription cap exhausted
    monkeypatch.setattr(gai, "transcribe_voice_gemini", AsyncMock(return_value="စမ်းသပ်ချက်"))
    monkeypatch.setattr(settings, "group_ai_voice_daily_cap", 2)
    gai.rate_limiter.record_voice_transcription()
    gai.rate_limiter.record_voice_transcription()
    assert not gai.rate_limiter.can_transcribe_voice(2)

    capped_msg = _fake_voice_message(duration=10, file_size=1024)
    await handle_group_message(capped_msg, state=_fake_state(), session=db_session, bot=capped_msg.bot)
    capped_msg.bot.get_file.assert_not_called()


@pytest.mark.asyncio
async def test_voice_toggle_disabled(db_session, monkeypatch):
    """When group_ai_voice_enabled=False, voice messages are ignored."""
    await _enable_group_ai(db_session)
    monkeypatch.setattr(settings, "group_ai_voice_enabled", False)

    msg = _fake_voice_message(duration=10, file_size=1024)
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    msg.bot.get_file.assert_not_called()
    msg.reply.assert_not_called()


# ===========================================================================
# Part 3: Admin Console & Knowledge Sheet Endpoints
# ===========================================================================


@pytest.mark.asyncio
async def test_admin_console_endpoints(db_session, monkeypatch):
    """GET and POST endpoints for knowledge sheet and group AI status."""
    from app.webapp.api import (
        GroupAiKnowledgeSheetPayload,
        get_admin_group_ai_console,
        reset_group_ai_knowledge_sheet,
        update_group_ai_knowledge_sheet,
    )

    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    admin_user = User(id=ADMIN_ID, username="lead_admin")

    # 1. GET initial status (built-in default)
    status = await get_admin_group_ai_console(admin=admin_user, db=db_session)
    assert status["is_custom"] is False
    assert status["char_limit"] == 8000
    assert "budgets" in status
    assert "recent_actions" in status

    # 2. POST save knowledge sheet
    new_sheet = "Voice Clone Pro သည် မြန်မာဘာသာဖြင့် AI voice cloning ပြုလုပ်ပေးသော platform ဖြစ်ပါသည်။"
    save_res = await update_group_ai_knowledge_sheet(
        GroupAiKnowledgeSheetPayload(sheet=new_sheet),
        admin=admin_user,
        db=db_session,
    )
    assert save_res["success"] is True
    assert save_res["is_custom"] is True
    assert save_res["char_count"] == len(new_sheet)

    # Check effective fact sheet uses the new sheet
    assert gai.get_effective_fact_sheet() == new_sheet

    # Verify audit log written
    audit_stmt = select(AuditLog).where(AuditLog.action == "group_ai_knowledge_sheet_update")
    audit = (await db_session.execute(audit_stmt)).scalar_one()
    assert audit.admin_id == ADMIN_ID

    # 3. POST reset restores built-in sheet
    reset_res = await reset_group_ai_knowledge_sheet(admin=admin_user, db=db_session)
    assert reset_res["success"] is True
    assert reset_res["is_custom"] is False
    assert gai.get_effective_fact_sheet() == gai.get_built_in_fact_sheet()


@pytest.mark.asyncio
async def test_knowledge_sheet_hard_cap_8000(monkeypatch):
    """Knowledge sheet rejects empty or > 8000 characters."""
    from fastapi import HTTPException
    from app.webapp.api import (
        GroupAiKnowledgeSheetPayload,
        update_group_ai_knowledge_sheet,
    )

    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    admin_user = User(id=ADMIN_ID, username="admin")

    # Empty sheet rejected
    with pytest.raises(HTTPException) as exc:
        await update_group_ai_knowledge_sheet(
            GroupAiKnowledgeSheetPayload(sheet="   "),
            admin=admin_user,
            db=MagicMock(),
        )
    assert exc.value.status_code == 400

    # Over 8000 chars rejected
    long_sheet = "A" * 8001
    with pytest.raises(HTTPException) as exc:
        await update_group_ai_knowledge_sheet(
            GroupAiKnowledgeSheetPayload(sheet=long_sheet),
            admin=admin_user,
            db=MagicMock(),
        )
    assert exc.value.status_code == 400


def test_guardrail_precedence_in_prompt():
    """System guardrails (Burmese-only, zero emoji, payment deferral, credits never expire) remain pinned."""
    # Even if custom sheet makes false claims, pinned guardrails in SYSTEM_INSTRUCTIONS win
    custom_sheet = "Credits expire after 30 days."
    prompt = gai.build_prompt(
        author_name="Aung",
        text="အသံသွင်းခွင့် သက်တမ်းရှိလား",
        bot_mentioned=True,
        is_reply_to_bot=False,
        fact_sheet=custom_sheet,
    )
    assert "Credits expire after 30 days." in prompt
    assert "Purchased minutes/credits NEVER expire" in gai.SYSTEM_INSTRUCTIONS
    assert "မြန်မာဘာသာ သက်သက်" in gai.SYSTEM_INSTRUCTIONS
    assert "Emoji လုံးဝ မပါရ" in gai.SYSTEM_INSTRUCTIONS


def test_cache_bust_version_is_r159():
    """Cache token bumped to 20261117_r160 across index.html."""
    from pathlib import Path
    index_html = (Path(__file__).parent.parent.parent / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
    assert index_html.count("?v=20261117_r160") == 4
    assert "20261116_r158" not in index_html
