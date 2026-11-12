"""Batch H pins for the group-AI upgrade (2026-11-03).

Covers: the limits retune (BotSetting-driven 60s cooldown, 40/hour
and 300/day AI-call budgets that only real AI calls consume),
escalation drafts with one-tap admin approval (first tap wins,
admins only, 24h expiry), the FAQ cache built ONLY from approved
answers, and the in-memory conversation context fed to the decision
prompt. The Gemini layer is stubbed everywhere — no network, no keys.
"""

import itertools
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatType
from aiogram.types import Message, User as TgUser
from sqlalchemy import select

from app.config import settings
from app.handlers.group_admin import (
    handle_escalation_callback,
    handle_group_ai_edit_text,
    handle_group_message,
)
from app.models.schema import BotSetting, GroupAiEscalation, GroupAiFaq
from app.services import group_ai_admin as gai

GROUP_ID = gai.COMMUNITY_GROUP_ID  # -1003795677289
ADMIN_ID = 424242
ADMIN2_ID = 434343


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(gai, "rate_limiter", gai.GroupReplyRateLimiter())
    monkeypatch.setattr(gai, "context_buffer", gai.GroupContextBuffer())


async def _set_kill_switch(db_session, value="true"):
    db_session.add(BotSetting(key=gai.KILL_SWITCH_KEY, value=value))
    await db_session.commit()


_sent_ids = itertools.count(9000)


def _fake_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=lambda *a, **k: SimpleNamespace(message_id=next(_sent_ids))
    )
    bot.edit_message_text = AsyncMock()
    bot.edit_message_reply_markup = AsyncMock()
    return bot


def _fake_message(text, *, user_id=555001, message_id=10, chat_id=GROUP_ID, reply_to=None):
    msg = MagicMock(spec=Message)
    msg.message_id = message_id
    msg.text = text
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.chat.type = ChatType.SUPERGROUP
    msg.from_user = TgUser(id=user_id, is_bot=False, first_name="Aung", username="aung")
    msg.sender_chat = None
    msg.reply_to_message = reply_to
    msg.reply = AsyncMock()
    msg.answer = AsyncMock()
    msg.bot = _fake_bot()
    return msg


def _fake_state():
    state = MagicMock()
    state.get_state = AsyncMock(return_value=None)
    return state


def _fake_callback(data, *, user_id, first_name="Admin"):
    cb = MagicMock()
    cb.data = data
    cb.from_user = TgUser(id=user_id, is_bot=False, first_name=first_name)
    cb.message = MagicMock()
    cb.message.edit_reply_markup = AsyncMock()
    cb.answer = AsyncMock()
    cb.bot = _fake_bot()
    return cb


def _fake_edit_state(data=None):
    state = MagicMock()
    state._data = dict(data or {})
    state.set_state = AsyncMock()
    state.clear = AsyncMock()
    state.get_data = AsyncMock(side_effect=lambda: dict(state._data))

    async def _update_data(**kwargs):
        state._data.update(kwargs)

    state.update_data = AsyncMock(side_effect=_update_data)
    return state


# ---------------------------------------------------------------------------
# Limits retune
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_limits_defaults_and_botsetting_overrides(db_session):
    limits = await gai.get_limits(db_session)
    assert limits.user_cooldown_seconds == 60
    assert limits.hourly_budget == 40
    assert limits.daily_budget == 300

    db_session.add(BotSetting(key="group_ai_user_cooldown_seconds", value="30"))
    db_session.add(BotSetting(key="group_ai_hourly_budget", value="5"))
    db_session.add(BotSetting(key="group_ai_daily_budget", value="junk"))
    await db_session.commit()

    tuned = await gai.get_limits(db_session)
    assert tuned.user_cooldown_seconds == 30  # config value honoured
    assert tuned.hourly_budget == 5
    assert tuned.daily_budget == 300  # garbage falls back to the default


def test_hourly_budget_counts_only_ai_calls():
    clock = _Clock()
    lim = gai.GroupReplyRateLimiter(now_fn=clock)
    # Replies (fast paths / FAQ hits / AI answers) never consume budget.
    for uid in range(50):
        assert lim.can_reply(1000 + uid, 2000 + uid)
        lim.record_reply(1000 + uid, 2000 + uid)
    assert lim.ai_budget_available()
    for _ in range(40):
        lim.record_ai_call()
    assert not lim.ai_budget_available()


def test_daily_budget_blocks_after_hourly_window_slides():
    clock = _Clock()
    lim = gai.GroupReplyRateLimiter(hourly_budget=1000, daily_budget=3, now_fn=clock)
    for _ in range(3):
        assert lim.ai_budget_available()
        lim.record_ai_call()
    assert not lim.ai_budget_available()
    clock.t = 3601  # hourly window long gone; the daily cap still binds
    assert not lim.ai_budget_available()
    clock.t = 86401  # a full day later the budget is back
    assert lim.ai_budget_available()


@pytest.mark.asyncio
async def test_sensitive_deferral_still_posts_when_budget_exhausted(db_session, monkeypatch):
    await _set_kill_switch(db_session)
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "မဖြေသင့်"))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    draft = AsyncMock(return_value="မူကြမ်း")
    monkeypatch.setattr(gai, "draft_escalation_answer", draft)
    for _ in range(40):  # burn the whole hourly AI budget
        gai.rate_limiter.record_ai_call()
    assert not gai.rate_limiter.ai_budget_available()

    msg = _fake_message("ငွေသွင်းပြီးပြီ၊ မိနစ် မဝင်သေး ဘူး", message_id=77)
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)

    ai.assert_not_called()  # hard rule: decision AI is never asked
    draft.assert_not_called()  # no budget -> no draft call either
    # ...but the deferral and the admin DM still go out, draft-free.
    msg.reply.assert_called_once_with(gai.DEFERRAL_TEXT_SENSITIVE)
    msg.bot.send_message.assert_called_once()
    _, kwargs = msg.bot.send_message.call_args
    assert kwargs.get("reply_markup") is None


@pytest.mark.asyncio
async def test_ai_answer_silent_when_budget_exhausted(db_session, monkeypatch):
    await _set_kill_switch(db_session)
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "အဖြေ"))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    for _ in range(40):
        gai.rate_limiter.record_ai_call()

    msg = _fake_message("Recap က ဘယ်လို လုပ်ရတာလဲ")
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    ai.assert_not_called()
    msg.reply.assert_not_called()


# ---------------------------------------------------------------------------
# Escalation drafts + one-tap approval
# ---------------------------------------------------------------------------


async def _make_escalation(
    db_session,
    *,
    draft="မူကြမ်း အဖြေ",
    age_hours=0,
    status="PENDING",
    copies=None,
    dm_text="DM မူရင်း စာသား",
):
    row = GroupAiEscalation(
        chat_id=GROUP_ID,
        message_id=77,
        author_id=555001,
        author_label="Aung",
        question_text="ငွေသွင်းပြီးပြီ၊ မိနစ် မဝင်သေး ဘူး",
        draft_text=draft,
        original_draft_text=draft,
        status=status,
        reason="sensitive",
    )
    if copies is not None:
        row.dm_text = dm_text
        row.dm_copies = json.dumps(copies)
    if age_hours:
        row.created_at = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    db_session.add(row)
    await db_session.commit()
    return row.id


@pytest.mark.asyncio
async def test_escalation_dm_carries_draft_and_buttons(db_session, monkeypatch):
    await _set_kill_switch(db_session)
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    draft = AsyncMock(return_value="Admin စစ်ဆေးနေပါတယ်၊ ပြေစာ ပုံ ပို့ပေးပါ။")
    monkeypatch.setattr(gai, "draft_escalation_answer", draft)

    msg = _fake_message("ငွေသွင်းပြီးပြီ၊ မိနစ် မဝင်သေး ဘူး", message_id=77)
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)

    msg.bot.send_message.assert_called_once()
    args, kwargs = msg.bot.send_message.call_args
    assert "AI အကြံပြု အဖြေ မူကြမ်း" in args[1]
    assert "ပြေစာ ပုံ ပို့ပေးပါ" in args[1]
    markup = kwargs.get("reply_markup")
    assert markup is not None
    flat = [b for row_ in markup.inline_keyboard for b in row_]
    actions = [gai.parse_escalation_callback(b.callback_data)[0] for b in flat]
    assert actions == ["approve", "edit", "dismiss"]
    _, esc_id = gai.parse_escalation_callback(flat[0].callback_data)

    rows = (await db_session.execute(select(GroupAiEscalation))).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == esc_id
    assert rows[0].status == "PENDING"
    assert rows[0].draft_text == "Admin စစ်ဆေးနေပါတယ်၊ ပြေစာ ပုံ ပို့ပေးပါ။"
    # The delivered DM copy's coordinates are stored for cross-admin sync.
    stored_copies = json.loads(rows[0].dm_copies)
    assert len(stored_copies) == 1
    assert stored_copies[0][0] == ADMIN_ID
    assert isinstance(stored_copies[0][1], int)
    assert rows[0].dm_text == args[1]


@pytest.mark.asyncio
async def test_approve_posts_draft_once_and_saves_faq(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", f"{ADMIN_ID},{ADMIN2_ID}")
    esc_id = await _make_escalation(db_session)

    cb = _fake_callback(gai.escalation_callback_data("approve", esc_id), user_id=ADMIN_ID)
    await handle_escalation_callback(cb, session=db_session, bot=cb.bot)
    cb.bot.send_message.assert_called_once()
    args, kwargs = cb.bot.send_message.call_args
    assert args[0] == GROUP_ID
    assert args[1] == "မူကြမ်း အဖြေ"
    assert kwargs.get("reply_to_message_id") == 77
    cb.answer.assert_called_once_with(gai.ADMIN_CONFIRM_POSTED)

    row = await db_session.get(GroupAiEscalation, esc_id)
    assert row.status == "APPROVED"
    assert row.handled_by == ADMIN_ID

    faqs = (await db_session.execute(select(GroupAiFaq))).scalars().all()
    assert len(faqs) == 1
    assert faqs[0].answer_text == "မူကြမ်း အဖြေ"

    # Second tap (another admin) is told it is handled; nothing reposts.
    cb2 = _fake_callback(gai.escalation_callback_data("approve", esc_id), user_id=ADMIN2_ID)
    await handle_escalation_callback(cb2, session=db_session, bot=cb2.bot)
    cb2.bot.send_message.assert_not_called()
    cb2.answer.assert_called_once_with(gai.ADMIN_ALERT_ALREADY_HANDLED, show_alert=True)


@pytest.mark.asyncio
async def test_dismiss_posts_nothing(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    esc_id = await _make_escalation(db_session)

    cb = _fake_callback(gai.escalation_callback_data("dismiss", esc_id), user_id=ADMIN_ID)
    await handle_escalation_callback(cb, session=db_session, bot=cb.bot)

    cb.bot.send_message.assert_not_called()
    cb.answer.assert_called_once_with(gai.ADMIN_CONFIRM_DISMISSED)
    row = await db_session.get(GroupAiEscalation, esc_id)
    assert row.status == "DISMISSED"
    faqs = (await db_session.execute(select(GroupAiFaq))).scalars().all()
    assert faqs == []


@pytest.mark.asyncio
async def test_non_admin_tap_rejected(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    esc_id = await _make_escalation(db_session)

    cb = _fake_callback(gai.escalation_callback_data("approve", esc_id), user_id=999999)
    await handle_escalation_callback(cb, session=db_session, bot=cb.bot)

    cb.bot.send_message.assert_not_called()
    cb.answer.assert_called_once_with(gai.ADMIN_ALERT_NOT_ADMIN, show_alert=True)
    row = await db_session.get(GroupAiEscalation, esc_id)
    assert row.status == "PENDING"  # untouched


@pytest.mark.asyncio
async def test_unknown_and_expired_ids_fail_gracefully(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))

    cb = _fake_callback(gai.escalation_callback_data("approve", 999999), user_id=ADMIN_ID)
    await handle_escalation_callback(cb, session=db_session, bot=cb.bot)
    cb.bot.send_message.assert_not_called()
    cb.answer.assert_called_once_with(gai.ADMIN_ALERT_NOT_FOUND, show_alert=True)

    stale_id = await _make_escalation(db_session, age_hours=25)
    cb2 = _fake_callback(gai.escalation_callback_data("approve", stale_id), user_id=ADMIN_ID)
    await handle_escalation_callback(cb2, session=db_session, bot=cb2.bot)
    cb2.bot.send_message.assert_not_called()
    cb2.answer.assert_called_once_with(gai.ADMIN_ALERT_EXPIRED, show_alert=True)
    row = await db_session.get(GroupAiEscalation, stale_id)
    assert row.status == "EXPIRED"


@pytest.mark.asyncio
async def test_sweep_expires_only_stale_pending(db_session):
    fresh_id = await _make_escalation(db_session)
    stale_id = await _make_escalation(db_session, age_hours=25)
    done_id = await _make_escalation(db_session, age_hours=30, status="APPROVED")

    swept = await gai.expire_stale_escalations(db_session)
    assert swept == 1
    assert (await db_session.get(GroupAiEscalation, fresh_id)).status == "PENDING"
    assert (await db_session.get(GroupAiEscalation, stale_id)).status == "EXPIRED"
    assert (await db_session.get(GroupAiEscalation, done_id)).status == "APPROVED"


def test_sensitive_draft_prompt_forbids_outcome_promises():
    prompt = gai.build_draft_prompt(author_name="Aung", text="refund လုပ်ပေးပါ", reason="sensitive")
    assert "Holding" in prompt
    assert "ကတိ လုံးဝ မပေးရပါ" in prompt  # never promise an outcome
    assert "ပြေစာ" in prompt  # asks for the details an admin needs


@pytest.mark.asyncio
async def test_draft_answer_strips_emoji_and_rejects_protocol(monkeypatch):
    monkeypatch.setattr(
        gai, "_ask_gemini_sync", lambda prompt, client_factory=None: "စောင့်ပေးပါနော် 🙂"
    )
    draft = await gai.draft_escalation_answer(text="မေးခွန်း", reason="ai_escalate")
    assert draft == "စောင့်ပေးပါနော်"

    monkeypatch.setattr(gai, "_ask_gemini_sync", lambda prompt, client_factory=None: "ESCALATE")
    assert await gai.draft_escalation_answer(text="မေးခွန်း", reason="sensitive") is None

    def _boom(prompt, client_factory=None):
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(gai, "_ask_gemini_sync", _boom)
    assert await gai.draft_escalation_answer(text="မေးခွန်း", reason="sensitive") is None


# ---------------------------------------------------------------------------
# FAQ cache
# ---------------------------------------------------------------------------


def test_faq_similarity_and_normalisation():
    assert gai.normalize_question("စျေး ဘယ်လောက် လဲ?! ❤️") == gai.normalize_question(
        "စျေး ဘယ်လောက် လဲ"
    )
    assert gai.faq_similarity("a b c", "a b c") == 1.0
    assert gai.faq_similarity("a b", "c d") == 0.0
    assert gai.faq_similarity("a b c d", "a b c d e") == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_faq_save_lookup_and_upsert(db_session):
    faq_id = await gai.save_faq_pair(
        db_session,
        question_text="စျေး ဘယ်လောက် လဲ ခင်ဗျာ",
        answer_text="Voice Pro က ၃၀,၀၀၀ ကျပ် ပါ။",
    )
    assert faq_id is not None

    hit = await gai.find_faq_answer(db_session, "စျေး ဘယ်လောက် လဲ ခင်ဗျာ")
    assert hit == (faq_id, "Voice Pro က ၃၀,၀၀၀ ကျပ် ပါ။")
    # Punctuation/case variant -> same normal form -> hit.
    hit2 = await gai.find_faq_answer(db_session, "စျေး ဘယ်လောက် လဲ ခင်ဗျာ!!")
    assert hit2 is not None and hit2[0] == faq_id
    # Superset sharing 4 of 5 tokens (Jaccard 0.8) -> hit.
    hit3 = await gai.find_faq_answer(db_session, "စျေး ဘယ်လောက် လဲ ခင်ဗျာ ပြောပြပါ")
    assert hit3 is not None and hit3[0] == faq_id
    # A dissimilar question misses.
    assert await gai.find_faq_answer(db_session, "Recap ဗီဒီယို ဘယ်လို လုပ်ရတာလဲ") is None

    # Re-approving the same question replaces the answer, no duplicate.
    await gai.save_faq_pair(
        db_session,
        question_text="စျေး ဘယ်လောက် လဲ ခင်ဗျာ",
        answer_text="Voice Pro က ၃၅,၀၀၀ ကျပ် ပါ။",
    )
    rows = (await db_session.execute(select(GroupAiFaq))).scalars().all()
    assert len(rows) == 1
    assert rows[0].answer_text == "Voice Pro က ၃၅,၀၀၀ ကျပ် ပါ။"


@pytest.mark.asyncio
async def test_handler_faq_hit_answers_without_ai(db_session, monkeypatch):
    await _set_kill_switch(db_session)
    faq_id = await gai.save_faq_pair(
        db_session,
        question_text="စျေး ဘယ်လောက် လဲ ခင်ဗျာ",
        answer_text="Voice Pro က ၃၀,၀၀၀ ကျပ် ပါ။",
    )
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "AI အဖြေ မသုံးသင့်"))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)

    msg = _fake_message("စျေး ဘယ်လောက် လဲ ခင်ဗျာ ပြောပြပါ")
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)

    ai.assert_not_called()  # the cache answered — no AI call, no budget
    msg.reply.assert_called_once_with("Voice Pro က ၃၀,၀၀၀ ကျပ် ပါ။")
    row = await db_session.get(GroupAiFaq, faq_id)
    assert row.hit_count == 1


# ---------------------------------------------------------------------------
# Conversation context
# ---------------------------------------------------------------------------


def test_prompt_includes_context_and_replied_to():
    prompt = gai.build_prompt(
        author_name="Bo",
        text="အဲဒါဆို ဘယ်လောက်လဲ",
        bot_mentioned=False,
        is_reply_to_bot=False,
        context_lines=["Aung: စျေး ဘယ်လောက် လဲ", "Aung: Voice Pro အကြောင်း"],
        replied_to_text="Voice Pro က ဘာတွေ ပါလဲ",
    )
    assert "အနီးဆုံး စကားဝိုင်း အခြေအနေ" in prompt
    assert "Aung: စျေး ဘယ်လောက် လဲ" in prompt
    assert "Reply လုပ်ထားတဲ့ မူရင်း စာ: Voice Pro က ဘာတွေ ပါလဲ" in prompt

    plain = gai.build_prompt(
        author_name="Bo", text="စျေး", bot_mentioned=False, is_reply_to_bot=False
    )
    assert "အနီးဆုံး စကားဝိုင်း အခြေအနေ" not in plain  # back-compatible


def test_context_buffer_cap_and_truncation():
    buf = gai.GroupContextBuffer()
    for i in range(18):
        buf.push(GROUP_ID, f"U{i}", f"စာ {i}")
    lines = buf.snapshot(GROUP_ID)
    assert len(lines) == 16  # capped at the newest 16 (Batch GA; was 10)
    assert lines[0] == "U2: စာ 2"
    assert lines[-1] == "U17: စာ 17"
    assert buf.snapshot(-100111) == []  # other chats unaffected

    buf.push(GROUP_ID, "Long", "က" * 400)
    assert len(buf.snapshot(GROUP_ID)[-1]) == len("Long: ") + 300


@pytest.mark.asyncio
async def test_handler_feeds_context_and_reply_to_ai(db_session, monkeypatch):
    await _set_kill_switch(db_session)
    calls = []

    async def _capture(**kwargs):
        calls.append(kwargs)
        return gai.DECISION_REPLY, "အဖြေ တစ်ခု"

    monkeypatch.setattr(gai, "ask_group_admin_ai", _capture)

    first = _fake_message("Voice Pro က ဘာတွေ ပါလဲ", user_id=555001, message_id=1)
    await handle_group_message(first, state=_fake_state(), session=db_session, bot=first.bot)
    assert calls[0]["context_lines"] == []  # nothing before the first message
    assert calls[0]["replied_to_text"] is None

    original = MagicMock(spec=Message)
    original.text = "Voice Pro က ဘာတွေ ပါလဲ"
    original.from_user = TgUser(id=555001, is_bot=False, first_name="Aung", username="aung")
    second = _fake_message(
        "အဲဒါဆို ဘယ်လောက်လဲ", user_id=666002, message_id=2, reply_to=original
    )
    await handle_group_message(second, state=_fake_state(), session=db_session, bot=second.bot)
    assert calls[1]["context_lines"] == ["Aung: Voice Pro က ဘာတွေ ပါလဲ"]
    assert calls[1]["replied_to_text"] == "Voice Pro က ဘာတွေ ပါလဲ"


# ---------------------------------------------------------------------------
# Edit flow + cross-admin keyboard sync (owner additions A + B)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_claim_blocks_other_admins(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", f"{ADMIN_ID},{ADMIN2_ID}")
    esc_id = await _make_escalation(
        db_session, copies=[[ADMIN_ID, 101], [ADMIN2_ID, 102]]
    )
    state = _fake_edit_state()
    cb = _fake_callback(
        gai.escalation_callback_data("edit", esc_id), user_id=ADMIN_ID, first_name="Zaw"
    )
    await handle_escalation_callback(cb, state=state, session=db_session, bot=cb.bot)

    row = await db_session.get(GroupAiEscalation, esc_id)
    assert row.status == "EDITING"
    assert row.handled_by == ADMIN_ID
    state.set_state.assert_awaited()
    assert state._data["escalation_id"] == esc_id

    # The claimer gets the FSM prompt DM with a cancel button.
    cb.bot.send_message.assert_called_once()
    args, kwargs = cb.bot.send_message.call_args
    assert args[0] == ADMIN_ID
    assert "ပြင်ရန်" in args[1]
    cancel_btn = kwargs["reply_markup"].inline_keyboard[0][0]
    assert gai.parse_escalation_callback(cancel_btn.callback_data) == ("cancel", esc_id)

    # Every stored copy was rewritten: status line appended, keys gone.
    assert cb.bot.edit_message_text.await_count == 2
    for call in cb.bot.edit_message_text.call_args_list:
        assert call.kwargs["reply_markup"] is None
        assert "✏️ Zaw ပြင်နေပါတယ်" in call.args[0]

    # Another admin's tap on a stale copy loses and clears that copy.
    cb2 = _fake_callback(
        gai.escalation_callback_data("approve", esc_id), user_id=ADMIN2_ID
    )
    await handle_escalation_callback(cb2, session=db_session, bot=cb2.bot)
    cb2.bot.send_message.assert_not_called()
    cb2.answer.assert_called_once_with(gai.ADMIN_ALERT_ALREADY_HANDLED, show_alert=True)
    cb2.message.edit_reply_markup.assert_awaited()
    row = await db_session.get(GroupAiEscalation, esc_id)
    assert row.status == "EDITING"  # the claim survives the stray tap


@pytest.mark.asyncio
async def test_edit_flow_replaces_draft_and_posts_edited(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", f"{ADMIN_ID},{ADMIN2_ID}")
    esc_id = await _make_escalation(
        db_session,
        draft="AI မူကြမ်း",
        copies=[[ADMIN_ID, 101], [ADMIN2_ID, 102]],
    )
    state = _fake_edit_state()
    cb = _fake_callback(gai.escalation_callback_data("edit", esc_id), user_id=ADMIN_ID)
    await handle_escalation_callback(cb, state=state, session=db_session, bot=cb.bot)

    # The claiming admin sends the corrected text in DM.
    msg = _fake_message("ပြင်ထားတဲ့ အဖြေ 🙂 အပြည့်", user_id=ADMIN_ID, chat_id=ADMIN_ID)
    await handle_group_ai_edit_text(msg, state=state, session=db_session, bot=msg.bot)

    row = await db_session.get(GroupAiEscalation, esc_id)
    assert row.status == "PENDING"  # claim released
    assert row.handled_by is None
    assert row.draft_text == "ပြင်ထားတဲ့ အဖြေ အပြည့်"  # edited + emoji-stripped
    assert row.original_draft_text == "AI မူကြမ်း"  # AI original kept for audit
    state.clear.assert_awaited()
    msg.answer.assert_called_once_with(gai.ADMIN_CONFIRM_EDIT_SAVED)

    # Fresh DMs with the updated draft + live keyboard went to ALL admins.
    assert msg.bot.send_message.await_count == 2
    for call in msg.bot.send_message.call_args_list:
        assert "ပြင်ထားတဲ့ အဖြေ အပြည့်" in call.args[1]
        assert call.kwargs["reply_markup"] is not None
    assert len(json.loads(row.dm_copies)) == 2  # new copies replaced the old

    # Approving now posts the EDITED text, and the FAQ caches the edit.
    cb2 = _fake_callback(
        gai.escalation_callback_data("approve", esc_id), user_id=ADMIN2_ID
    )
    await handle_escalation_callback(cb2, session=db_session, bot=cb2.bot)
    cb2.bot.send_message.assert_called_once()
    args, kwargs = cb2.bot.send_message.call_args
    assert args[0] == GROUP_ID
    assert args[1] == "ပြင်ထားတဲ့ အဖြေ အပြည့်"
    faqs = (await db_session.execute(select(GroupAiFaq))).scalars().all()
    assert len(faqs) == 1
    assert faqs[0].answer_text == "ပြင်ထားတဲ့ အဖြေ အပြည့်"


@pytest.mark.asyncio
async def test_edit_cancel_releases_claim_and_restores_keyboard(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", f"{ADMIN_ID},{ADMIN2_ID}")
    esc_id = await _make_escalation(
        db_session, draft="မူလ မူကြမ်း", copies=[[ADMIN_ID, 101], [ADMIN2_ID, 102]]
    )
    state = _fake_edit_state()
    cb = _fake_callback(gai.escalation_callback_data("edit", esc_id), user_id=ADMIN_ID)
    await handle_escalation_callback(cb, state=state, session=db_session, bot=cb.bot)

    cb_cancel = _fake_callback(
        gai.escalation_callback_data("cancel", esc_id), user_id=ADMIN_ID
    )
    await handle_escalation_callback(
        cb_cancel, state=state, session=db_session, bot=cb_cancel.bot
    )

    row = await db_session.get(GroupAiEscalation, esc_id)
    assert row.status == "PENDING"
    assert row.handled_by is None
    assert row.draft_text == "မူလ မူကြမ်း"  # untouched
    state.clear.assert_awaited()
    cb_cancel.answer.assert_called_once_with(gai.ADMIN_CONFIRM_EDIT_CANCELLED)
    # Fresh copies with the original draft + keyboard restored for all.
    assert cb_cancel.bot.send_message.await_count == 2
    for call in cb_cancel.bot.send_message.call_args_list:
        assert "မူလ မူကြမ်း" in call.args[1]
        assert call.kwargs["reply_markup"] is not None

    # A non-owner cannot cancel someone else's claim.
    esc2 = await _make_escalation(db_session)
    state2 = _fake_edit_state()
    cb_claim = _fake_callback(
        gai.escalation_callback_data("edit", esc2), user_id=ADMIN_ID
    )
    await handle_escalation_callback(
        cb_claim, state=state2, session=db_session, bot=cb_claim.bot
    )
    cb_other = _fake_callback(
        gai.escalation_callback_data("cancel", esc2), user_id=ADMIN2_ID
    )
    await handle_escalation_callback(cb_other, session=db_session, bot=cb_other.bot)
    cb_other.answer.assert_called_once_with(gai.ADMIN_ALERT_NOT_EDITOR, show_alert=True)
    row2 = await db_session.get(GroupAiEscalation, esc2)
    assert row2.status == "EDITING"
    assert row2.handled_by == ADMIN_ID


@pytest.mark.asyncio
async def test_terminal_sync_edits_every_copy_and_tolerates_failure(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", f"{ADMIN_ID},{ADMIN2_ID}")
    esc_id = await _make_escalation(
        db_session, copies=[[ADMIN_ID, 101], [ADMIN2_ID, 102]]
    )
    cb = _fake_callback(
        gai.escalation_callback_data("approve", esc_id),
        user_id=ADMIN_ID,
        first_name="Zaw",
    )
    attempts = []

    async def _flaky_edit(text, **kwargs):
        attempts.append((text, kwargs))
        if len(attempts) == 1:
            raise RuntimeError("message is too old to edit")

    cb.bot.edit_message_text = AsyncMock(side_effect=_flaky_edit)
    await handle_escalation_callback(cb, session=db_session, bot=cb.bot)

    # The action itself succeeded despite the first copy failing.
    cb.bot.send_message.assert_called_once()  # the group post
    row = await db_session.get(GroupAiEscalation, esc_id)
    assert row.status == "APPROVED"
    # Both copies were attempted, with the posted status line on the text.
    assert len(attempts) == 2
    for text, kwargs in attempts:
        assert "✅ Zaw က တင်ပြီးပါပြီ" in text
        assert kwargs["reply_markup"] is None
    # The failed copy fell back to a markup-only clear.
    cb.bot.edit_message_reply_markup.assert_awaited()
    fb = cb.bot.edit_message_reply_markup.call_args
    assert fb.kwargs["chat_id"] == ADMIN_ID
    assert fb.kwargs["message_id"] == 101
