"""Batch AQ pins (2026-11-05): Group AI reliability.

Production failure (2026-11-05 evening): maintenance mode was ON and
a member asked "ခုထိ သုံးမရသေးဘူး" in the community group. The bot
received it, but the Gemini decision call burned the entire 45 s
timeout walking the (now 17-key x 4-model) pool and died — the log
line ended in an empty colon (a bare TimeoutError stringifies to "")
and the member got silence. Two more maintenance questions that day
also went unanswered, and because decisions were logged NOWHERE it
was impossible to tell failure from a deliberate NO_REPLY.

Batch AQ:
* a deterministic maintenance fast path (availability detector +
  canned Batch Z maintenance answer) that never touches the AI;
* the group decision walk capped at AI_MAX_ATTEMPTS attempts;
* one decision log line per terminal outcome, and the AI-failure
  warning naming the exception type. DECISION_AI_FAILED separates a
  failed CALL from an AI-chosen NO_REPLY in the bookkeeping (the
  member-visible silence is unchanged).
"""

import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatType
from aiogram.types import Message, User as TgUser

from app.config import settings
from app.handlers.group_admin import handle_group_message
from app.models.schema import BotSetting, GroupAiFaq
from app.services import group_ai_admin as gai
from app.services import key_rotation

GROUP_ID = gai.COMMUNITY_GROUP_ID  # -1003795677289
NOTE = "ည ၉:၃၀ ပြန်ဖွင့်ပါမယ်"

REAL_Q1 = "Bot က ပြန်သုံးလို့မရသေးဘူးလား"  # 2026-11-05 07:54
REAL_Q2 = "ခုထိ သုံးမရသေးဘူး"  # 2026-11-05 20:12 (the timed-out one)
REAL_Q3 = "ခုထိရော ??"  # 2026-11-05 10:25 (context-only follow-up)


# ---------------------------------------------------------------------------
# Fixtures / fakes (same harness shape as test_group_ai_admin_2026_11_02)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(gai, "rate_limiter", gai.GroupReplyRateLimiter())
    monkeypatch.setattr(gai, "context_buffer", gai.GroupContextBuffer())
    old_mode = settings.maintenance_mode
    old_note = settings.maintenance_note
    yield
    settings.maintenance_mode = old_mode
    settings.maintenance_note = old_note


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


async def _set_kill_switch(db_session, value="true"):
    db_session.add(BotSetting(key=gai.KILL_SWITCH_KEY, value=value))
    await db_session.commit()


def _maintenance(monkeypatch, *, on=True, note=""):
    monkeypatch.setattr(settings, "maintenance_mode", on)
    monkeypatch.setattr(settings, "maintenance_note", note)


# ---------------------------------------------------------------------------
# Detector: positives (the real 2026-11-05 questions + close variants)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [REAL_Q1, REAL_Q2])
def test_detector_matches_the_real_questions(text):
    assert gai.is_service_availability_question(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "app ဖွင့်လို့မရဘူး",
        "Mini App ဝင်လို့ မရဘူး",
        "ဘယ်တော့ ပြန်ဖွင့်မလဲ",
        "ပြုပြင်နေတုန်းပဲလား",
        "service is down, not working",
        "can't use the bot",
        "when will it be back",
    ],
)
def test_detector_matches_availability_variants(text):
    assert gai.is_service_availability_question(text) is True


# ---------------------------------------------------------------------------
# Detector: negatives — no genuine availability signal, no fast path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "မင်္ဂလာပါ",  # greeting
        "စျေး ဘယ်လောက် လဲ",  # price question
        "ငွေလွှဲထားတာ မဝင်သေးဘူး",  # payment (also escalates first)
        "ဟဟ ရယ်ရတယ်",  # member-to-member fragment
        "အေး ငါလည်း မရဘူး",  # member chat with a bare negation
        "မရသေးဘူး",  # bare "not yet" — deliberately NOT a signal
        "အသံ ဘယ်နှစ် မျိုး ရှိလဲ",  # unrelated service question
        "စမ်းချင်တယ်",  # starter fragment
        "Recap က ဘယ်လို လုပ်ရတာလဲ",  # how-to question
    ],
)
def test_detector_negatives(text):
    assert gai.is_service_availability_question(text) is False


def test_detector_still_fragment_needs_context():
    # Alone, "ခုထိရော ??" is a context-only follow-up: no fast path.
    assert gai.is_service_availability_question(REAL_Q3) is False
    assert (
        gai.is_service_availability_question(REAL_Q3, context_lines=["ဟဟ ရယ်ရတယ်"])
        is False
    )
    # With an availability signal in the recent window it is covered.
    assert (
        gai.is_service_availability_question(REAL_Q3, context_lines=[REAL_Q1]) is True
    )
    # ... or when it replies to an availability message.
    assert (
        gai.is_service_availability_question(REAL_Q3, replied_to_text=REAL_Q2) is True
    )


# ---------------------------------------------------------------------------
# Maintenance reply builder
# ---------------------------------------------------------------------------


def test_maintenance_reply_quotes_note_verbatim(monkeypatch):
    _maintenance(monkeypatch, on=True, note=NOTE)
    reply = gai.build_maintenance_reply()
    assert NOTE in reply
    assert "ပြုပြင်ထိန်းသိမ်းမှု" in reply


def test_maintenance_reply_without_note_invents_no_time(monkeypatch):
    _maintenance(monkeypatch, on=True, note="")
    reply = gai.build_maintenance_reply()
    assert "ကြေညာ မထားရသေးပါဘူး" in reply


def test_maintenance_reply_has_no_emoji(monkeypatch):
    _maintenance(monkeypatch, on=True, note=NOTE)
    reply = gai.build_maintenance_reply()
    assert gai.strip_emoji(reply) == reply


def test_maintenance_mode_on_reads_settings(monkeypatch):
    _maintenance(monkeypatch, on=True)
    assert gai.maintenance_mode_on() is True
    _maintenance(monkeypatch, on=False)
    assert gai.maintenance_mode_on() is False


# ---------------------------------------------------------------------------
# Bounded AI walk
# ---------------------------------------------------------------------------


@pytest.fixture
def _gemini_health_reset():
    health = key_rotation.health_for("gemini")
    health.reset()
    yield
    health.reset()


def _patch_pool(monkeypatch, keys):
    pool = [(f"L{i + 1}", key) for i, key in enumerate(keys)]
    monkeypatch.setattr(gai, "collect_gemini_key_pool", lambda: pool)
    monkeypatch.setattr(gai, "gemini_model_chain", lambda: ["m1", "m2", "m3", "m4"])


def test_walk_cap_bounds_attempts(monkeypatch, _gemini_health_reset):
    _patch_pool(monkeypatch, ["k1", "k2", "k3"])
    calls = []

    class _Models:
        def generate_content(self, model=None, contents=None):
            calls.append(model)
            raise RuntimeError("boom")

    class _Client:
        models = _Models()

    with pytest.raises(RuntimeError, match="after 8 attempts"):
        gai._ask_gemini_sync("hi", client_factory=lambda key: _Client())
    assert len(calls) == gai.AI_MAX_ATTEMPTS == 8
    assert gai.AI_MAX_ATTEMPTS < 17 * 4  # the cap is the point of the batch


def test_walk_success_on_second_key(monkeypatch, _gemini_health_reset):
    _patch_pool(monkeypatch, ["k1", "k2"])
    used = []

    class _Resp:
        text = "REPLY: ရပါတယ်"

    class _Models:
        def __init__(self, key):
            self._key = key

        def generate_content(self, model=None, contents=None):
            if self._key == "k1":
                raise RuntimeError("boom")
            return _Resp()

    class _Client:
        def __init__(self, key):
            self.models = _Models(key)

    def factory(api_key):
        used.append(api_key)
        return _Client(api_key)

    assert gai._ask_gemini_sync("hi", client_factory=factory) == "REPLY: ရပါတယ်"
    assert used == ["k1", "k2"]


@pytest.mark.asyncio
async def test_ask_pool_exhaustion_returns_ai_failed(monkeypatch, caplog, _gemini_health_reset):
    _patch_pool(monkeypatch, ["k1"])

    class _Models:
        def generate_content(self, model=None, contents=None):
            raise RuntimeError("boom")

    class _Client:
        models = _Models()

    with caplog.at_level(logging.WARNING, logger="app.services.group_ai_admin"):
        decision, text = await gai.ask_group_admin_ai(
            text="မေးခွန်း", client_factory=lambda key: _Client()
        )
    assert (decision, text) == (gai.DECISION_AI_FAILED, None)
    assert gai.DECISION_AI_FAILED != gai.DECISION_NO_REPLY
    assert "RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_ask_timeout_returns_ai_failed_and_logs_type(
    monkeypatch, caplog, _gemini_health_reset
):
    def _slow(prompt, *, client_factory=None):
        time.sleep(1.5)
        return "REPLY: နောက်ကျပြီ"

    monkeypatch.setattr(gai, "_ask_gemini_sync", _slow)
    monkeypatch.setattr(gai, "AI_TIMEOUT_SECONDS", 0.2)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="app.services.group_ai_admin"):
        decision, text = await gai.ask_group_admin_ai(text="မေးခွန်း")
    elapsed = time.monotonic() - started
    assert (decision, text) == (gai.DECISION_AI_FAILED, None)
    assert elapsed < 1.5  # wait_for cut it off well inside the old 45 s world
    assert "TimeoutError" in caplog.text  # the type is named, not an empty colon


# ---------------------------------------------------------------------------
# Handler: the maintenance fast path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fast_path_answers_without_ai_or_budget(db_session, monkeypatch):
    await _set_kill_switch(db_session)
    _maintenance(monkeypatch, on=True, note=NOTE)
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "AI အဖြေ မသုံးရ"))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    msg = _fake_message(REAL_Q2)
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    ai.assert_not_called()
    msg.reply.assert_called_once()
    sent = msg.reply.call_args[0][0]
    assert NOTE in sent
    assert "ပြုပြင်ထိန်းသိမ်းမှု" in sent
    # No AI budget consumed by the fixed reply.
    assert gai.rate_limiter.snapshot_state()["ai_calls_wall"] == []


@pytest.mark.asyncio
async def test_fast_path_answers_first_real_question(db_session, monkeypatch):
    await _set_kill_switch(db_session)
    _maintenance(monkeypatch, on=True)
    ai = AsyncMock(return_value=(gai.DECISION_NO_REPLY, None))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    msg = _fake_message(REAL_Q1)
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    ai.assert_not_called()
    msg.reply.assert_called_once_with(gai.build_maintenance_reply())


@pytest.mark.asyncio
async def test_fast_path_never_fires_when_maintenance_off(db_session, monkeypatch):
    await _set_kill_switch(db_session)
    _maintenance(monkeypatch, on=False)
    ai = AsyncMock(return_value=(gai.DECISION_NO_REPLY, None))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    msg = _fake_message(REAL_Q2)
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    ai.assert_called_once()  # falls through to the AI path as before
    msg.reply.assert_not_called()


@pytest.mark.asyncio
async def test_fast_path_repeat_suppressed_by_cooldown(db_session, monkeypatch):
    await _set_kill_switch(db_session)
    _maintenance(monkeypatch, on=True)
    ai = AsyncMock(return_value=(gai.DECISION_NO_REPLY, None))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    first = _fake_message(REAL_Q2, message_id=1)
    second = _fake_message("app ဖွင့်လို့မရဘူး", message_id=2)
    await handle_group_message(first, state=_fake_state(), session=db_session, bot=first.bot)
    await handle_group_message(second, state=_fake_state(), session=db_session, bot=second.bot)
    first.reply.assert_called_once()
    second.reply.assert_not_called()  # 60 s per-user cooldown still applies
    ai.assert_not_called()


@pytest.mark.asyncio
async def test_fast_path_still_fragment_via_group_context(db_session, monkeypatch):
    await _set_kill_switch(db_session)
    _maintenance(monkeypatch, on=True)
    ai = AsyncMock(return_value=(gai.DECISION_NO_REPLY, None))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    first = _fake_message(REAL_Q1, user_id=555001, message_id=1)
    second = _fake_message(REAL_Q3, user_id=555002, message_id=2)
    await handle_group_message(first, state=_fake_state(), session=db_session, bot=first.bot)
    await handle_group_message(second, state=_fake_state(), session=db_session, bot=second.bot)
    first.reply.assert_called_once()
    # The follow-up "ခုထိရော ??" is answered from context, not the AI.
    second.reply.assert_called_once_with(gai.build_maintenance_reply())
    ai.assert_not_called()


@pytest.mark.asyncio
async def test_payment_still_escalates_with_maintenance_on(db_session, monkeypatch):
    await _set_kill_switch(db_session)
    _maintenance(monkeypatch, on=True)
    monkeypatch.setattr(settings, "admin_ids_raw", "424242")
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "မဖြေသင့်တဲ့ အဖြေ"))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    msg = _fake_message("ငွေသွင်းပြီးပြီ၊ မိနစ် မဝင်သေး ဘူး ခင်ဗျာ", message_id=77)
    await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    ai.assert_not_called()  # sensitive check runs before the fast path
    msg.reply.assert_called_once_with(gai.DEFERRAL_TEXT_SENSITIVE)


# ---------------------------------------------------------------------------
# Handler: decision logging (ids + reason class only — never the text)
# ---------------------------------------------------------------------------


def _assert_no_message_text(caplog, *texts):
    for record in caplog.records:
        rendered = record.getMessage()
        for text in texts:
            assert text not in rendered


@pytest.mark.asyncio
async def test_log_fast_path_maintenance(db_session, monkeypatch, caplog):
    await _set_kill_switch(db_session)
    _maintenance(monkeypatch, on=True)
    monkeypatch.setattr(
        gai, "ask_group_admin_ai", AsyncMock(return_value=(gai.DECISION_NO_REPLY, None))
    )
    msg = _fake_message(REAL_Q2, message_id=909)
    with caplog.at_level(logging.INFO, logger="app.handlers.group_admin"):
        await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    assert "decision=fast_path:maintenance" in caplog.text
    assert "message_id=909" in caplog.text
    _assert_no_message_text(caplog, REAL_Q2)


@pytest.mark.asyncio
async def test_log_reply_and_no_reply(db_session, monkeypatch, caplog):
    await _set_kill_switch(db_session)
    _maintenance(monkeypatch, on=False)
    question = "Recap က ဘယ်လို လုပ်ရတာလဲ"
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "Bot ကို ဖွင့်ပြီး စလို့ ရပါတယ်။"))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    msg = _fake_message(question, message_id=1)
    with caplog.at_level(logging.INFO, logger="app.handlers.group_admin"):
        await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    assert "decision=reply" in caplog.text

    caplog.clear()
    ai2 = AsyncMock(return_value=(gai.DECISION_NO_REPLY, None))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai2)
    msg2 = _fake_message(question, user_id=555009, message_id=2)
    with caplog.at_level(logging.INFO, logger="app.handlers.group_admin"):
        await handle_group_message(msg2, state=_fake_state(), session=db_session, bot=msg2.bot)
    assert "decision=no_reply" in caplog.text
    assert "reason=ai_decision" in caplog.text
    _assert_no_message_text(caplog, question)


@pytest.mark.asyncio
async def test_log_escalate_sensitive(db_session, monkeypatch, caplog):
    await _set_kill_switch(db_session)
    monkeypatch.setattr(settings, "admin_ids_raw", "424242")
    monkeypatch.setattr(
        gai, "ask_group_admin_ai", AsyncMock(return_value=(gai.DECISION_REPLY, "x"))
    )
    msg = _fake_message("ငွေသွင်းပြီးပြီ၊ မိနစ် မဝင်သေး ဘူး ခင်ဗျာ", message_id=78)
    with caplog.at_level(logging.INFO, logger="app.handlers.group_admin"):
        await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    assert "decision=escalate" in caplog.text
    assert "reason=sensitive" in caplog.text


@pytest.mark.asyncio
async def test_log_ai_failed_not_no_reply(db_session, monkeypatch, caplog):
    await _set_kill_switch(db_session)
    _maintenance(monkeypatch, on=False)

    def _boom(prompt, *, client_factory=None):
        raise RuntimeError("pool dead")

    monkeypatch.setattr(gai, "_ask_gemini_sync", _boom)
    monkeypatch.setattr(gai, "collect_gemini_key_pool", lambda: [("L1", "k1")])
    msg = _fake_message("Recap က ဘယ်လို လုပ်ရတာလဲ", message_id=55)
    with caplog.at_level(logging.INFO):
        await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    msg.reply.assert_called_once_with(gai.HOLDING_REPLY_AI_FAILED)
    assert "decision=ai_failed" in caplog.text
    assert "decision=no_reply" not in caplog.text
    assert "RuntimeError" in caplog.text  # the service warning names the type


@pytest.mark.asyncio
async def test_log_faq_hit(db_session, monkeypatch, caplog):
    await _set_kill_switch(db_session)
    _maintenance(monkeypatch, on=False)
    question = "VIP plan ဆိုတာ ဘာလဲ"
    db_session.add(
        GroupAiFaq(
            question_norm=gai.normalize_question(question),
            question_text=question,
            answer_text="VIP plan ဆိုတာ အထူး plan ပါ။",
        )
    )
    await db_session.commit()
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "AI အဖြေ မသုံးရ"))
    monkeypatch.setattr(gai, "ask_group_admin_ai", ai)
    msg = _fake_message(question, message_id=31)
    with caplog.at_level(logging.INFO, logger="app.handlers.group_admin"):
        await handle_group_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    ai.assert_not_called()
    msg.reply.assert_called_once_with("VIP plan ဆိုတာ အထူး plan ပါ။")
    assert "decision=faq_hit" in caplog.text
    _assert_no_message_text(caplog, question)
