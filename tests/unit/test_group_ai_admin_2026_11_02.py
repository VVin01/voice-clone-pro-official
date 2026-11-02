"""Pins for the AI group-admin auto-reply (2026-11-02).

Covers the whole safety envelope of app/services/group_ai_admin.py and
app/handlers/group_admin.py: pre-filter, kill switch (default OFF),
sensitive-topic hard rule with owner escalation, rate limits with an
injectable clock, the NO_REPLY/ESCALATE/REPLY protocol parser, the
fact sheet's grounding in the real plan catalogue, and chat scoping.
The Gemini call is stubbed everywhere — no network, no keys.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatType
from aiogram.types import Message, User as TgUser

from app.config import settings
from app.handlers.group_admin import handle_group_message
from app.models.schema import BotSetting
from app.services import group_ai_admin as gai

GROUP_ID = gai.COMMUNITY_GROUP_ID  # -1003795677289


def _prefilter(**overrides):
    fields = dict(
        chat_id=GROUP_ID,
        chat_type="supergroup",
        text="Voice Clone က ဘယ်လို သုံးရတာလဲ ခင်ဗျာ",
        from_user_id=555001,
        from_user_is_bot=False,
        sender_chat_id=None,
    )
    fields.update(overrides)
    return gai.prefilter_reason(**fields)


# ---------------------------------------------------------------------------
# Pre-filter
# ---------------------------------------------------------------------------


def test_prefilter_accepts_real_question():
    assert _prefilter() is None


def test_prefilter_ignores_other_chat():
    assert _prefilter(chat_id=-1001112223334) == "other_chat"


def test_prefilter_ignores_private_chat():
    assert _prefilter(chat_type="private") == "not_a_group_chat"


def test_prefilter_ignores_bot_author():
    assert _prefilter(from_user_is_bot=True) == "bot_or_anonymous_author"
    assert _prefilter(from_user_id=None) == "bot_or_anonymous_author"


def test_prefilter_ignores_admin_author(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", "777001")
    assert _prefilter(from_user_id=777001) == "admin_author"


def test_prefilter_ignores_sender_chat_post():
    assert _prefilter(sender_chat_id=GROUP_ID) == "sender_chat_post"


def test_prefilter_ignores_commands_and_trivia():
    assert _prefilter(text="/start") == "command"
    assert _prefilter(text="/admin settings") == "command"
    assert _prefilter(text="က") == "too_short"
    assert _prefilter(text="   ") == "too_short"
    assert _prefilter(text=None) == "no_text"
    assert _prefilter(text="😂😂😂") == "no_speakable_content"
    assert _prefilter(text="👍👍") == "no_speakable_content"


def test_resolve_group_chat_id_configured_wins(monkeypatch):
    # Unconfigured: fall back to the community constant.
    monkeypatch.setattr(settings, "required_group_id", "")
    assert gai.resolve_group_chat_id() == GROUP_ID
    assert gai.is_community_group(GROUP_ID)
    # Configured gate id wins over the constant.
    monkeypatch.setattr(settings, "required_group_id", "-1001234567890")
    assert gai.resolve_group_chat_id() == -1001234567890
    assert gai.is_community_group(-1001234567890)
    assert not gai.is_community_group(GROUP_ID)


# ---------------------------------------------------------------------------
# Rate limiter (injectable clock)
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_rate_limiter_user_cooldown():
    clock = _Clock()
    lim = gai.GroupReplyRateLimiter(now_fn=clock)
    assert lim.can_reply(1, 100)
    lim.record_reply(1, 100)
    clock.t = 60
    assert not lim.can_reply(1, 101)  # same user, inside 180s cooldown
    assert lim.can_reply(2, 101)  # a different user is unaffected
    clock.t = 181
    assert lim.can_reply(1, 102)


def test_rate_limiter_global_cap_and_window():
    clock = _Clock()
    lim = gai.GroupReplyRateLimiter(now_fn=clock)
    for uid in range(10):
        assert lim.can_reply(1000 + uid, 2000 + uid)
        lim.record_reply(1000 + uid, 2000 + uid)
    assert not lim.can_reply(9999, 3000)  # 11th reply inside the hour
    clock.t = 3601
    assert lim.can_reply(9999, 3000)  # window has slid past


def test_rate_limiter_never_answers_same_message_twice():
    clock = _Clock()
    lim = gai.GroupReplyRateLimiter(now_fn=clock)
    lim.record_reply(1, 42)
    clock.t = 100000  # cooldown and window long gone
    assert not lim.can_reply(1, 42)
    assert lim.already_answered(42)


def test_rate_limiter_deferral_cooldown():
    clock = _Clock()
    lim = gai.GroupReplyRateLimiter(now_fn=clock)
    assert lim.can_defer(7)
    lim.record_deferral(7)
    assert not lim.can_defer(7)
    clock.t = 181
    assert lim.can_defer(7)


# ---------------------------------------------------------------------------
# AI decision protocol
# ---------------------------------------------------------------------------


def test_parse_ai_decision():
    assert gai.parse_ai_decision("NO_REPLY") == (gai.DECISION_NO_REPLY, None)
    assert gai.parse_ai_decision("  no_reply  ") == (gai.DECISION_NO_REPLY, None)
    assert gai.parse_ai_decision("```\nNO_REPLY\n```") == (gai.DECISION_NO_REPLY, None)
    assert gai.parse_ai_decision("ESCALATE") == (gai.DECISION_ESCALATE, None)
    assert gai.parse_ai_decision("REPLY: မင်္ဂလာပါ ခင်ဗျာ") == (
        gai.DECISION_REPLY,
        "မင်္ဂလာပါ ခင်ဗျာ",
    )
    # Off-protocol output fails silent, never leaks into the group.
    assert gai.parse_ai_decision("မင်္ဂလာပါ၊ ကူညီပါရစေ") == (gai.DECISION_NO_REPLY, None)
    assert gai.parse_ai_decision("REPLY:") == (gai.DECISION_NO_REPLY, None)
    assert gai.parse_ai_decision("") == (gai.DECISION_NO_REPLY, None)
    assert gai.parse_ai_decision(None) == (gai.DECISION_NO_REPLY, None)


# ---------------------------------------------------------------------------
# Fact sheet grounding
# ---------------------------------------------------------------------------


def test_fact_sheet_built_from_ground_truth():
    from app.services.payments import PLAN_CATALOGUE

    sheet = gai.build_fact_sheet()
    for entry in PLAN_CATALOGUE:
        assert f"{entry['price_mmk']:,}" in sheet
    assert "180 စက္ကန့်" in sheet  # free voice allowance (settings default)
    assert "Recap Credit 3 ခု" in sheet  # one-time free recap credits
    assert "တစ်နေ့ Job 5 ခု" in sheet  # free daily job limit
    assert "@VoiceClonePro" in sheet
    assert "Video Upload" in sheet  # blocked-source guidance
    # The sheet must forbid inventing facts beyond it.
    assert "ထွင်မပြောရ" in sheet


def test_fact_sheet_tracks_settings(monkeypatch):
    monkeypatch.setattr(settings, "free_daily_job_limit", 9)
    assert "တစ်နေ့ Job 9 ခု" in gai.build_fact_sheet()


# ---------------------------------------------------------------------------
# Sensitive topics (hard rule)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Refund လုပ်ပေးပါ",
        "ငွေသွင်းပြီးပြီ၊ ဒါပေမယ့် မိနစ် မဝင်သေး ဘူး",
        "ငွေလွှဲပြီး ပြေစာ ပို့ထားတယ်၊ approve မလုပ်သေး ဘူး",
        "I paid but my minutes are not approved yet",
        "အကောင့် ပိတ်ခံရတယ်၊ ဘာဖြစ်တာလဲ",
        "credit မရ ဘူး ဖြစ်နေတယ်",
    ],
)
def test_sensitive_topics_detected(text):
    assert gai.detect_sensitive_topic(text)


@pytest.mark.parametrize(
    "text",
    [
        "စျေး ဘယ်လောက် လဲ ခင်ဗျာ",
        "ငွေသွင်းနည်း ပြောပြပေးပါ",  # general how-to-pay is an FAQ, not a dispute
        "Voice Pro က ဘယ်လောက် လဲ",
        "Free က ဘယ်လောက် ရလဲ",
        "ဘယ်လို စသုံးရမလဲ",
    ],
)
def test_sensitive_topics_not_overbroad(text):
    assert not gai.detect_sensitive_topic(text)


# ---------------------------------------------------------------------------
# Links, key pool, model chain
# ---------------------------------------------------------------------------


def test_group_message_link():
    assert gai.group_message_link(-1003795677289, 123) == "https://t.me/c/3795677289/123"


def test_collect_gemini_key_pool_order_and_dedupe():
    env = {
        "GEMINI_API_KEY": "k-single",
        "GEMINI_API_KEYS": "k-csv1, k-csv2, k-single",
        "GEMINI_API_KEY_2": "k-two",
        "GEMINI_API_KEY_10": "k-ten",
        "GEMINI_IMAGE_PROJECT_3_KEY": "k-proj3",
        "GEMINI_IMAGE_PROJECT_1_KEY": "k-proj1",
        "UNRELATED": "nope",
    }
    pool = gai.collect_gemini_key_pool(env)
    labels = [label for label, _ in pool]
    # The single key also appears inside the CSV, so dedupe keeps the
    # CSV occurrence (planner semantics: dedupe by value, first wins)
    # and the trailing GEMINI_API_KEY add contributes nothing new.
    assert labels == [
        "GEMINI_IMAGE_PROJECT_1_KEY",
        "GEMINI_IMAGE_PROJECT_3_KEY",
        "GEMINI_API_KEY_2",
        "GEMINI_API_KEY_10",
        "GEMINI_API_KEYS[1]",
        "GEMINI_API_KEYS[2]",
        "GEMINI_API_KEYS[3]",
    ]
    values = [value for _, value in pool]
    assert values == ["k-proj1", "k-proj3", "k-two", "k-ten", "k-csv1", "k-csv2", "k-single"]
    # Labels must never carry key material.
    assert all("k-" not in label for label in labels)


def test_gemini_model_chain_prefers_settings():
    chain = gai.gemini_model_chain()
    assert chain[0] == settings.gemini_primary_model
    assert len(chain) == len(set(chain))


# ---------------------------------------------------------------------------
# Handler behaviour (Gemini stubbed)
# ---------------------------------------------------------------------------


def _fake_message(text, *, user_id=555001, message_id=10, chat_id=GROUP_ID):
    msg = MagicMock(spec=Message)
    msg.message_id = message_id
    msg.text = text
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.chat.type = ChatType.SUPERGROUP
    msg.from_user = TgUser(id=user_id, is_bot=False, first_name="Aung", username="aung")
    msg.sender_chat = None
    msg.reply_to_message = None
    msg.reply = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.send_message = AsyncMock()
    return msg


def _fake_state():
    state = MagicMock()
    state.get_state = AsyncMock(return_value=None)
    return state


@pytest.fixture(autouse=True)
def _fresh_limiter(monkeypatch):
    monkeypatch.setattr(gai, "rate_limiter", gai.GroupReplyRateLimiter())


async def _set_kill_switch(db_session, value):
    db_session.add(BotSetting(key=gai.KILL_SWITCH_KEY, value=value))
    await db_session.commit()


@pytest.mark.asyncio
async def test_handler_silent_when_kill_switch_off(db_session, monkeypatch):
    monkeypatch.setattr(settings, "group_ai_admin_enabled", False)
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "ဖြေပေးလိုက်မယ်"))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    msg = _fake_message("ဘယ်လို စသုံးရမလဲ ခင်ဗျာ")
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    msg.reply.assert_not_called()
    ai.assert_not_called()  # default OFF means the AI is never even asked


@pytest.mark.asyncio
async def test_handler_replies_when_enabled(db_session, monkeypatch):
    await _set_kill_switch(db_session, "true")
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "Bot ကို ဖွင့်ပြီး စလို့ ရပါတယ်။"))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    msg = _fake_message("Recap က ဘယ်လို လုပ်ရတာလဲ")
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    msg.reply.assert_called_once_with("Bot ကို ဖွင့်ပြီး စလို့ ရပါတယ်။")


@pytest.mark.asyncio
async def test_handler_ignores_other_chat(db_session, monkeypatch):
    await _set_kill_switch(db_session, "true")
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "..."))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    msg = _fake_message("ဘယ်လို စသုံးရမလဲ", chat_id=-1009998887776)
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    msg.reply.assert_not_called()
    ai.assert_not_called()


@pytest.mark.asyncio
async def test_handler_no_reply_decision_stays_silent(db_session, monkeypatch):
    await _set_kill_switch(db_session, "true")
    ai = AsyncMock(return_value=(gai.DECISION_NO_REPLY, None))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    msg = _fake_message("ဟဟ ရယ်ရတယ်")  # off-topic chatter the AI declines
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    msg.reply.assert_not_called()
    msg.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_handler_payment_question_escalates_without_ai(db_session, monkeypatch):
    await _set_kill_switch(db_session, "true")
    monkeypatch.setattr(settings, "admin_ids_raw", "424242")
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "မဖြေသင့်တဲ့ အဖြေ"))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    msg = _fake_message("ငွေသွင်းပြီးပြီ၊ မိနစ် မဝင်သေး ဘူး ခင်ဗျာ", message_id=77)
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    ai.assert_not_called()  # hard rule: the AI is never asked
    msg.reply.assert_called_once_with(gai.DEFERRAL_TEXT_SENSITIVE)
    # Owner DM carries the question and a working group-message link.
    msg.bot.send_message.assert_called_once()
    args, kwargs = msg.bot.send_message.call_args
    assert args[0] == 424242
    assert "ငွေသွင်းပြီးပြီ" in args[1]
    assert "https://t.me/c/3795677289/77" in args[1]
    assert kwargs.get("parse_mode") == "HTML"


@pytest.mark.asyncio
async def test_handler_ai_escalation_defers_and_notifies(db_session, monkeypatch):
    await _set_kill_switch(db_session, "true")
    monkeypatch.setattr(settings, "admin_ids_raw", "424242")
    ai = AsyncMock(return_value=(gai.DECISION_ESCALATE, None))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    msg = _fake_message("Enterprise အတွက် သီးသန့် စျေး ရှိလား", message_id=88)
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    msg.reply.assert_called_once_with(gai.DEFERRAL_TEXT_UNSURE)
    msg.bot.send_message.assert_called_once()
    assert "https://t.me/c/3795677289/88" in msg.bot.send_message.call_args[0][1]


@pytest.mark.asyncio
async def test_handler_user_cooldown_blocks_second_reply(db_session, monkeypatch):
    await _set_kill_switch(db_session, "true")
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "အဖြေ"))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    first = _fake_message("စျေး ဘယ်လောက် လဲ", message_id=1)
    second = _fake_message("Free က ဘယ်လောက် ရလဲ", message_id=2)
    await handle_group_message(first, state=_fake_state(), session=db_session, bot=first.bot)
    await handle_group_message(second, state=_fake_state(), session=db_session, bot=second.bot)
    first.reply.assert_called_once()
    second.reply.assert_not_called()


@pytest.mark.asyncio
async def test_kill_switch_row_false_means_silence(db_session, monkeypatch):
    await _set_kill_switch(db_session, "false")
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "အဖြေ"))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    msg = _fake_message("စျေး ဘယ်လောက် လဲ")
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    msg.reply.assert_not_called()
    ai.assert_not_called()


def test_kill_switch_registered_as_dynamic_setting():
    from app.services.dynamic_settings import DYNAMIC_SETTING_KEYS, SETTING_TYPES

    assert SETTING_TYPES[gai.KILL_SWITCH_KEY] is bool
    assert gai.KILL_SWITCH_KEY in DYNAMIC_SETTING_KEYS
    assert settings.group_ai_admin_enabled is False  # ships OFF


# ---------------------------------------------------------------------------
# Social fast paths + tone (owner revision 2026-11-02)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["မင်္ဂလာပါ", "မင်္ဂလာပါ ❤️", "Hi", "hello", "Hey", "မင်္ဂလာပါ မင်္ဂလာပါ"],
)
def test_greeting_only_detected(text):
    assert gai.is_greeting_only(text)


@pytest.mark.parametrize(
    "text",
    [
        "မင်္ဂလာပါ၊ စျေး ဘယ်လောက် လဲ",  # greeting + real question → AI path
        "စျေး ဘယ်လောက် လဲ",
        "😂😂",
        "",
        "hi guys, recap ဘယ်လို လုပ်လဲ",
    ],
)
def test_greeting_only_rejects_non_greetings(text):
    assert not gai.is_greeting_only(text)


@pytest.mark.parametrize(
    "text",
    [
        "Want to try first par.",
        "want to try ❤️",
        "စမ်းချင်တယ်",
        "စမ်းကြည့်ချင်ပါတယ်",
        "သုံးချင်လို့ပါ",
        "how to start",
        "recap",
        "အသံပြောင်း",
        "just joined",
    ],
)
def test_starter_fragment_detected(text):
    assert gai.is_starter_fragment(text)


@pytest.mark.parametrize(
    "text",
    [
        "download မရဘူး",  # a problem report, not a starter fragment
        "စမ်းကြည့်တာ မရဘူး ဖြစ်နေတယ်",
        "စျေး ဘယ်လောက် လဲ",
        "want to try but it is not working",
        "ဒီ bot က ဘာတွေ လုပ်လို့ရလဲ၊ ဘာတွေ ရှိလဲ၊ သတ်မှတ်ချက်တွေ က ဘာတွေလဲ အကုန် သိချင်ပါတယ် ခင်ဗျာ ရှင်းပြပေးပါ",  # long real question
        "",
    ],
)
def test_starter_fragment_rejects_questions_and_problems(text):
    assert not gai.is_starter_fragment(text)


def _emoji_count(text: str) -> int:
    import re as _re

    return len(_re.findall("[\U0001F000-\U0001FAFF☀-➿]", text))


def test_welcome_and_pointer_copy_tone():
    for copy in (gai.WELCOME_TEXT, gai.STARTER_POINTER_TEXT):
        assert copy.count("ခင်ဗျာ") == 0  # no stiff formality pile-up
        assert _emoji_count(copy) <= 1  # at most one emoji
        assert "!!" not in copy
        assert "@voiceclonepro_bot" in copy  # always points at the bot
    assert "အခမဲ့" in gai.STARTER_POINTER_TEXT  # mentions the free trial
    assert "pin ထိုးထား" in gai.WELCOME_TEXT  # points at pinned tutorials


def test_deferral_copy_tone():
    for copy in (gai.DEFERRAL_TEXT_SENSITIVE, gai.DEFERRAL_TEXT_UNSURE):
        assert copy.count("ခင်ဗျာ") == 0
        assert _emoji_count(copy) <= 1


def test_prompt_tone_and_decision_spec():
    prompt = gai.SYSTEM_INSTRUCTIONS
    # New voice spec: sweet/gentle, ခင်ဗျာ capped, no over-the-top.
    assert "ချိုချိုသာသာ" in prompt
    assert "အများဆုံး တစ်ကြိမ်" in prompt  # the ခင်ဗျာ cap
    assert "အိုဗာ မဖြစ်ရပါ" in prompt
    # New decision spec: member-to-member chat is the silence case,
    # NOT "everything by default".
    assert "အဖွဲ့ဝင် အချင်းချင်း" in prompt
    assert "စကားဝိုင်း အားလုံးကို မဖြေရပါ" not in prompt
    # Hard rules survive the rewrite.
    assert "ESCALATE" in prompt
    assert "ထွင်မပြောရပါ" in prompt


def test_fact_sheet_knowledge_coverage():
    sheet = gai.build_fact_sheet()
    # Product map: tabs + downloader + custom voice.
    assert "Voice Studio" in sheet and "VIP" in sheet
    assert "Custom Voice" in sheet
    assert "Instagram" in sheet and "Douyin" in sheet
    # Personas derived from the curated catalogue + Gemini voices.
    assert "ကိုမင်း" in sheet and "မနှင်း" in sheet
    assert "ကိုသီဟ" in sheet and "မသွန်းရည်" in sheet
    # Recap knowledge: styles, engines, split rule, watermark rule.
    assert "POV" in sheet and "Funny" in sheet
    assert "Split" in sheet and "Credit 1 ခု စီ" in sheet
    assert "Watermark" in sheet
    # Refer programme constants from the ledger.
    assert "30 စက္ကန့်" in sheet and "20 ယောက်" in sheet
    # Membership gate + tutorial pointer.
    assert "Join" in sheet and "@VoiceClonePro" in sheet


@pytest.mark.asyncio
async def test_handler_greeting_gets_welcome_without_ai(db_session, monkeypatch):
    await _set_kill_switch(db_session, "true")
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "AI မဖြေသင့်"))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    msg = _fake_message("မင်္ဂလာပါ")
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    ai.assert_not_called()
    msg.reply.assert_called_once_with(gai.WELCOME_TEXT)


@pytest.mark.asyncio
async def test_handler_fragment_gets_pointer_without_ai(db_session, monkeypatch):
    await _set_kill_switch(db_session, "true")
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "AI မဖြေသင့်"))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    msg = _fake_message("Want to try first par. ❤️")
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    ai.assert_not_called()
    msg.reply.assert_called_once_with(gai.STARTER_POINTER_TEXT)


@pytest.mark.asyncio
async def test_handler_greeting_silent_when_switch_off(db_session, monkeypatch):
    monkeypatch.setattr(settings, "group_ai_admin_enabled", False)
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "..."))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    msg = _fake_message("မင်္ဂလာပါ")
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    msg.reply.assert_not_called()
    ai.assert_not_called()
