"""Part C (2026-11-25) tests: Group AI disparagement/competitor moderation & 1/3/6-month fact sheet."""
from __future__ import annotations

import asyncio
import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.handlers import group_moderation as gm_handlers
from app.handlers.group_admin import handle_group_message, handle_moderation_ban_callback
from app.models.schema import AuditLog, Base, BotSetting, GroupAiModerationStrike
from app.services import group_ai_admin as gai
from app.services import key_rotation as kr

GROUP_ID = gai.COMMUNITY_GROUP_ID
ADMIN_ID = 424242
REGULAR_USER_ID = 777333

_sent_ids = itertools.count(9100)


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        session.add(BotSetting(key=gai.KILL_SWITCH_KEY, value="true"))
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
def _fresh_rate_limiter(monkeypatch):
    monkeypatch.setattr(gai, "rate_limiter", gai.GroupReplyRateLimiter())
    monkeypatch.setattr(gai, "context_buffer", gai.GroupContextBuffer())


def _fake_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=lambda *a, **k: SimpleNamespace(message_id=next(_sent_ids)))
    bot.delete_message = AsyncMock()
    bot.restrict_chat_member = AsyncMock()
    bot.ban_chat_member = AsyncMock()
    bot.edit_message_text = AsyncMock()
    return bot


def _fake_message(text: str, *, user_id: int = REGULAR_USER_ID, message_id: int = 201, first_name: str = "Kyaw🔥"):
    msg = MagicMock(spec=Message)
    msg.message_id = message_id
    msg.text = text
    msg.voice = None
    msg.chat = MagicMock(id=GROUP_ID, type=ChatType.SUPERGROUP)
    msg.from_user = TgUser(id=user_id, is_bot=False, first_name=first_name, username="kyaw_test")
    msg.sender_chat = None
    msg.reply_to_message = None
    msg.reply = AsyncMock()
    msg.answer = AsyncMock()
    msg.bot = _fake_bot()
    return msg


def _fake_state():
    state = MagicMock()
    state.get_state = AsyncMock(return_value=None)
    return state


async def _drain_deletes():
    tasks = list(gm_handlers._pending_delete_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def test_moderation_prompt_includes_disparagement_competitor_and_guardrails():
    assert "disparagement" in gai.MODERATION_CATEGORIES
    assert "competitor" in gai.MODERATION_CATEGORIES
    instr = gai.SYSTEM_INSTRUCTIONS
    assert "disparagement:" in instr
    assert "competitor:" in instr
    assert "category=<selling|bashing|harm|disparagement|competitor|none>" in instr
    assert "X နဲ့ ဘာကွာလဲ" in instr
    # Female speech-style lock & zero emoji on moderator notices
    for strike_num in (1, 2, 3):
        for cat in ("disparagement", "competitor"):
            notice = gai.build_moderator_notice(
                category=cat, strike_num=strike_num, first_name="Su🔥Su", username="susu"
            )
            assert gai.strip_emoji(notice) == notice
            assert "🔥" not in notice
            assert "SuSu" in notice
            assert "ခင်ဗျာ" not in notice
            assert "ဗျာ" not in notice
            assert ("ရှင်" in notice or "ရှင့်" in notice)
    # Token limits preserved
    assert 2000 <= kr.estimate_prompt_tokens(instr) <= 3100
    assert kr.estimate_prompt_tokens(f"{instr}\n\n{gai.build_fact_sheet()}") < 9500


@pytest.mark.asyncio
async def test_disparagement_hit_deletes_posts_60s_notice_and_escalates_with_undo(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    assert gm_handlers.GROUP_NOTICE_TTL_SECONDS == 60
    monkeypatch.setattr(gm_handlers, "GROUP_NOTICE_TTL_SECONDS", 0.05)

    # Strike 1 (deterministic disparagement hit)
    m1 = _fake_message("ဒီ bot က scam ပါ အတုကြီး မဝယ်ကြနဲ့", message_id=301)
    await handle_group_message(m1, state=_fake_state(), session=db_session, bot=m1.bot)
    m1.bot.delete_message.assert_awaited_once_with(chat_id=GROUP_ID, message_id=301)
    assert len(gm_handlers._pending_delete_tasks) == 1
    m1.reply.assert_not_called()
    assert m1.bot.send_message.await_count == 1
    notice_call = m1.bot.send_message.await_args
    assert notice_call.kwargs["chat_id"] == GROUP_ID
    assert "Kyaw" in notice_call.kwargs["text"]
    assert gai.strip_emoji(notice_call.kwargs["text"]) == notice_call.kwargs["text"]
    assert "ခင်ဗျာ" not in notice_call.kwargs["text"]
    assert ("ရှင်" in notice_call.kwargs["text"] or "ရှင့်" in notice_call.kwargs["text"])
    await _drain_deletes()
    assert m1.bot.delete_message.await_count == 2  # offending message + 60s TTL notice

    # Strike 2 (AI verdict disparagement hit -> 1h mute)
    monkeypatch.setattr(
        gai,
        "ask_group_admin_ai",
        AsyncMock(return_value=gai.DecisionResult(gai.DECISION_NO_REPLY, None, {"category": "disparagement", "confidence": 0.92})),
    )
    m2 = _fake_message("ဒီဝန်ဆောင်မှုက အမှိုက်ပါပဲ group ထဲက ထွက်ကြ", message_id=302)
    await handle_group_message(m2, state=_fake_state(), session=db_session, bot=m2.bot)
    m2.bot.delete_message.assert_awaited_once_with(chat_id=GROUP_ID, message_id=302)
    m2.bot.restrict_chat_member.assert_awaited_once()
    await _drain_deletes()

    # Strike 3 (24h mute + Admin DM with gai_ban: -> AuditLog + gmod_undo:unban:)
    m3 = _fake_message("everyone leave this group it is total scam", message_id=303)
    await handle_group_message(m3, state=_fake_state(), session=db_session, bot=m3.bot)
    m3.bot.restrict_chat_member.assert_awaited_once()
    assert m3.bot.send_message.await_count == 2  # 1 group notice + 1 admin escalation DM
    admin_dm = m3.bot.send_message.await_args_list[1]
    assert admin_dm.args[0] == ADMIN_ID
    kb = admin_dm.kwargs["reply_markup"]
    ban_cb_data = kb.inline_keyboard[0][0].callback_data
    assert ban_cb_data.startswith("gai_ban:")

    # Execute gai_ban callback -> writes AuditLog & 1-tap gmod_undo:unban: button
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin", username="lead_admin")
    cb.data = ban_cb_data
    cb.bot = _fake_bot()
    cb.message = MagicMock()
    cb.message.html_text = "Escalation"
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    await handle_moderation_ban_callback(cb, bot=cb.bot, session=db_session)
    cb.bot.ban_chat_member.assert_awaited_once_with(chat_id=GROUP_ID, user_id=REGULAR_USER_ID)
    edit_kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert edit_kb.inline_keyboard[0][0].callback_data == f"gmod_undo:unban:{GROUP_ID}:{REGULAR_USER_ID}"
    audit_row = (await db_session.execute(select(AuditLog).where(AuditLog.action == "group_mod.gai_ban"))).scalar_one()
    assert audit_row.target_id == str(REGULAR_USER_ID)
    strikes = await gai.get_user_strikes_in_window(db_session, REGULAR_USER_ID)
    assert [s.action for s in strikes] == ["warn", "mute_1h", "mute_24h"]


@pytest.mark.asyncio
async def test_competitor_promo_and_delete_permission_failsafe(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    monkeypatch.setattr(gm_handlers, "GROUP_NOTICE_TTL_SECONDS", 0.01)

    m = _fake_message("Use OtherVoiceBot instead of this bot, way cheaper", message_id=401)
    # Fail-safe when bot lacks delete_message permission
    m.bot.delete_message = AsyncMock(side_effect=RuntimeError("not enough rights to delete"))
    await handle_group_message(m, state=_fake_state(), session=db_session, bot=m.bot)

    # Notice & strike STILL succeed cleanly without raising
    assert m.bot.send_message.await_count == 1
    assert "Kyaw" in m.bot.send_message.await_args.kwargs["text"]
    strikes = await gai.get_user_strikes_in_window(db_session, REGULAR_USER_ID)
    assert len(strikes) == 1 and strikes[0].category == "competitor"
    await _drain_deletes()


@pytest.mark.asyncio
async def test_false_positive_guardrails_no_strike_on_frustration_neutral_compare_or_low_conf(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))

    # 1. Frustrated bug / job-failure complaint -> NO strike, helpful reply
    monkeypatch.setattr(
        gai,
        "ask_group_admin_ai",
        AsyncMock(return_value=gai.DecisionResult(gai.DECISION_REPLY, "အလုပ်ပျက်သွားရင် Credit အလိုအလျောက် ပြန်ဝင်ပါတယ်ရှင်။")),
    )
    m_bug = _fake_message("ဒီနေ့ voice clone လုပ်တာ Error တက်နေတယ် မရဘူး စိတ်ညစ်တယ်", user_id=REGULAR_USER_ID, message_id=501)
    await handle_group_message(m_bug, state=_fake_state(), session=db_session, bot=m_bug.bot)
    m_bug.bot.delete_message.assert_not_awaited()
    m_bug.reply.assert_awaited_once()

    # 2. Neutral good-faith competitor comparison -> NO strike, normal reply
    monkeypatch.setattr(
        gai,
        "ask_group_admin_ai",
        AsyncMock(return_value=gai.DecisionResult(gai.DECISION_REPLY, "Voice Clone Pro က မြန်မာဘာသာနဲ့ Recap တွဲလုပ်နိုင်ပါတယ်ရှင်။")),
    )
    m_cmp = _fake_message("ElevenLabs နဲ့ ဘာကွာလဲ", user_id=REGULAR_USER_ID + 1, message_id=502)
    await handle_group_message(m_cmp, state=_fake_state(), session=db_session, bot=m_cmp.bot)
    m_cmp.bot.delete_message.assert_not_awaited()
    m_cmp.reply.assert_awaited_once()

    # 3. Confidence < threshold with helpful reply -> NO strike, helpful reply delivered
    monkeypatch.setattr(
        gai,
        "ask_group_admin_ai",
        AsyncMock(
            return_value=gai.DecisionResult(
                gai.DECISION_REPLY,
                "စျေးနှုန်းတွေကို Buy Tab မှာ ကြည့်နိုင်ပါတယ်ရှင်။",
                {"category": "competitor", "confidence": 0.6},
            )
        ),
    )
    m_low = _fake_message("တခြားဟာတွေထက် စျေးသက်သာလား", user_id=REGULAR_USER_ID + 2, message_id=503)
    await handle_group_message(m_low, state=_fake_state(), session=db_session, bot=m_low.bot)
    m_low.bot.delete_message.assert_not_awaited()
    m_low.reply.assert_awaited_once_with("စျေးနှုန်းတွေကို Buy Tab မှာ ကြည့်နိုင်ပါတယ်ရှင်။")

    for uid in (REGULAR_USER_ID, REGULAR_USER_ID + 1, REGULAR_USER_ID + 2):
        assert len(await gai.get_user_strikes_in_window(db_session, uid)) == 0


def test_built_in_fact_sheet_has_exact_2026_11_25_prices_and_1_3_6_month_validities():
    sheet = gai.build_fact_sheet()
    instr = gai.SYSTEM_INSTRUCTIONS

    # Zero "သက်တမ်း မကုန်" or blanket no-expiry claim on current plans
    assert "သက်တမ်း မကုန်" not in sheet
    assert "သက်တမ်း မကုန်" not in instr
    assert "သက်တမ်းမကုန် (No Expiry)" not in sheet
    assert "Grandfathered Balance များ သက်တမ်းမကုန်" not in instr

    # All 10 current plans with exact prices and 1/3/6-month validities + 1 Credit = 1 min rule
    expected_snippets = [
        "Starter (Voice 100 Mins + Recap 15 Credits · သက်တမ်း 1 လ): 14,900 ကျပ်",
        "Creator (Voice 200 Mins + Recap 30 Credits · သက်တမ်း 1 လ): 28,500 ကျပ်",
        "Pro (Voice 500 Mins + Recap 60 Credits · သက်တမ်း 3 လ): 54,900 ကျပ်",
        "Business (Voice 1000 Mins + Recap 180 Credits · သက်တမ်း 6 လ): 114,900 ကျပ်",
        "200 (Voice 200 Mins · သက်တမ်း 1 လ): 19,800 ကျပ်",
        "500 (Voice 500 Mins · သက်တမ်း 3 လ): 39,500 ကျပ်",
        "1000 (Voice 1000 Mins (+200 Bonus = 1200 Mins) · သက်တမ်း 6 လ): 79,200 ကျပ်",
        "30 (Recap 30 Credits · သက်တမ်း 1 လ): 13,500 ကျပ်",
        "60 (Recap 60 Credits · သက်တမ်း 3 လ): 25,500 ကျပ်",
        "180 (Recap 180 Credits · သက်တမ်း 6 လ): 68,400 ကျပ်",
        "1 Credit = Final Recap Output 1 မိနစ်",
    ]
    for snippet in expected_snippets:
        assert snippet in sheet, f"Missing expected built-in sheet line: {snippet}"
