"""Unit tests for Section C — Group AI Silence Fix Phase 1 (2026-11-24).

Pins:
1. Fixtures: `repro_yuki_413` (16-msg Burmese context buffer 200-300c each + 300c replied_to_text
   + 3 consecutive messages from Yuki), 4,000-char single message, 15,000-char custom sheet.
2. Token gates: assembled prompts for ALL fixtures (Group & Private) measure
   `estimate_prompt_tokens() <= 7,500` AND stay `<= 12,000` under a token-enforcing fake Groq
   at a 1.4x Myanmar multiplier (including `+ max_tokens`); `max_tokens <= 1024` on both providers.
3. Reactive 413 self-heal: fake Groq that 413s anything > 6,000 tokens + fake Gemini that 503s
   on every key -> trimmed Groq minimal-tier retry succeeds with REPLY (no silence, no Gemini).
4. Fallback speed/order: Groq down + Gemini 3.6/3.5 at 503 + lite healthy -> answer arrives via
   lite-first in < 5 s (mocked clock); total dual-outage wall-clock <= 25 s with no attempt
   starting past its deadline; `_default_client_factory` carries `timeout <= 10_000` & `attempts == 1`.
5. Loud/silent matrix: ambient failure -> 0 group replies, 0 DMs; Yuki fixture (3 consecutive
   direct failures) -> 1 holding reply (exact copy pin, `ရှင်`, zero `ဗျာ`/`ခင်ဗျာ`, zero emoji)
   + 1 Admin DM with `draft_escalation_answer` call count == 0; Private-AI failure -> 1 holding
   reply + 1 DM; consecutive failures don't repeatedly drain AI budget or duplicate context buffer.
6. Dedupe inventory pin: all 21 load-bearing rules from pre-fix `SYSTEM_INSTRUCTIONS` present,
   static baseline (`SYSTEM_INSTRUCTIONS + build_fact_sheet()`) < 9,500 est. tokens.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatType
from aiogram.types import Message, User as TgUser

from app.config import settings
from app.handlers.group_admin import handle_group_message, handle_private_message
from app.models.schema import BotSetting
from app.services import group_ai_admin as gai
from app.services import key_rotation as kr
from app.services import private_ai as pai

EXACT_HOLDING_COPY = (
    "ခဏလေး စောင့်ပေးပါနော်ရှင့် — အခု ချက်ချင်း ပြန်မဖြေနိုင်သေးလို့ "
    "Admin ကို အသိပေးထားပေးပါတယ်။ အရေးကြီးရင် @VoiceCloneProAdmin ဆီ "
    "တိုက်ရိုက် မေးနိုင်ပါတယ်ရှင့်။"
)


def _myanmar_1_4x_tokens(prompt: str) -> int:
    ascii_chars = sum(1 for ch in prompt if ord(ch) < 128)
    non_ascii = len(prompt) - ascii_chars
    return int(ascii_chars / 4.0 + 1.4 * non_ascii)


class _FakeHttpResp:
    def __init__(self, status_code: int, body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._body = body or {}
        self.text = text or json.dumps(self._body)

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _reset_limiters_and_pools(monkeypatch):
    kr.health_for(kr.GROQ_TEXT_PROVIDER).reset()
    kr.health_for("gemini").reset()
    monkeypatch.setattr(gai, "rate_limiter", gai.GroupReplyRateLimiter())
    monkeypatch.setattr(gai, "context_buffer", gai.GroupContextBuffer())
    monkeypatch.setattr(pai, "private_rate_limiter", gai.GroupReplyRateLimiter())
    monkeypatch.setattr(settings, "group_ai_knowledge_sheet", "")
    yield
    kr.health_for(kr.GROQ_TEXT_PROVIDER).reset()
    kr.health_for("gemini").reset()


def _build_repro_yuki_fixture() -> dict:
    base_turn = (
        "မင်္ဂလာပါ Voice Clone Pro မှာ ကိုယ်ပိုင်အသံနဲ့ Recap ဗီဒီယို လုပ်ချင်လို့ပါ၊ "
        "စျေးနှုန်းနဲ့ သက်တမ်းလေး သိချင်ပါတယ်၊ ဘယ် Plan က အကိုက်ဆုံး ဖြစ်မလဲ ပြောပြပေးပါနော်။"
    )  # ~160 chars; pad to ~240 chars
    padded_turn = base_turn + " အသံဖိုင်က ၁၅ စက္ကန့်လောက် ရှိပြီးသားပါ၊ TikTok လင့်ခ်နဲ့ စမ်းချင်ပါတယ်။"
    assert 200 <= len(padded_turn) <= 300
    context_lines = [f"Member{i + 1}: {padded_turn}" for i in range(13)] + [
        f"Yuki: {padded_turn}",
        f"Yuki: အပေါ်မှာ မေးထားတာလေး ပြန်ဖြေပေးပါဦးနော်၊ Recap Credit ဘယ်လို ဖြတ်တာလဲ သိချင်လို့ပါ။ " + ("က" * 120),
        f"Yuki: @voiceclonepro_bot ဖြေပေးပါဦးရှင့်၊ Starter Plan က သက်တမ်း ဘယ်နှစ်ရက် ရတာလဲ။ " + ("ခ" * 130),
    ]
    assert len(context_lines) == 16
    replied_to = ("Voice Clone Pro Starter Plan နဲ့ Recap Credit သုံးနည်း အသေးစိတ် မေးမြန်းထားသော စာသား — " * 4)[:300]
    assert len(replied_to) == 300
    return {
        "context_lines": context_lines,
        "replied_to_text": replied_to,
        "consecutive_messages": [
            "Recap က ကိုယ့်အသံနဲ့ လုပ်ရင် Credit ဘယ်လို ဖြတ်လဲ?",
            "Starter Plan သက်တမ်း ဘယ်နှစ်ရက်လဲ ဖြေပေးပါဦး",
            "@voiceclonepro_bot မဖြေသေးဘူးနော် ကူညီပါဦး",
        ],
    }


def test_fixtures_token_gates_1_4x_multiplier_and_max_tokens_cap(monkeypatch):
    """Pins #1 & #2: all fixtures <= 7,500 est. tokens AND <= 12,000 at 1.4x Myanmar multiplier; max_tokens <= 1024."""
    yuki = _build_repro_yuki_fixture()
    long_4k_msg = ("ကျွန်တော် မေးချင်တာက အသံပွားတဲ့အခါ စာသားအရှည်ကြီး ထည့်ရင် " * 70) + " နောက်ဆုံး မေးခွန်းက စျေး ဘယ်လောက်လဲ?"
    assert len(long_4k_msg) >= 4000
    assert long_4k_msg.endswith("စျေး ဘယ်လောက်လဲ?")

    custom_15k_structured = (
        "=== KNOWLEDGE SHEET ===\n"
        "【ထုတ်ကုန် အကြောင်း】\n" + ("• ထုတ်ကုန် အချက်အလက် အသေးစိတ် စာသား။ " * 140) + "\n"
        "【စျေးနှုန်း နဲ့ Free】\n" + ("• အထူး စျေးနှုန်း စာရင်း အရှည်ကြီး။ " * 200) + "\n"
        "【မကြာခဏ ဖြစ်တတ်တာတွေ】\n" + ("• ပြဿနာ ဖြေရှင်းနည်း လမ်းညွှန်ချက်။ " * 140) + "\n"
        "=== KNOWLEDGE SHEET END ==="
    )
    custom_15k_unstructured = "Voice Clone Pro အချက်အလက် အသေးစိတ် စာပိုဒ်ရှည်ကြီး — " * 320
    assert len(custom_15k_structured) >= 15000
    assert len(custom_15k_unstructured) >= 15000

    group_prompts = [
        # Static baseline (fitted)
        gai.build_prompt(author_name="Aung", text="မင်္ဂလာပါ စျေးသိချင်ပါတယ်", bot_mentioned=False, is_reply_to_bot=False),
        # repro_yuki_413
        gai.build_prompt(
            author_name="Yuki",
            text=yuki["consecutive_messages"][-1],
            bot_mentioned=True,
            is_reply_to_bot=True,
            context_lines=yuki["context_lines"],
            replied_to_text=yuki["replied_to_text"],
        ),
        # 4,000-char message (tail preserved!)
        gai.build_prompt(
            author_name="Bo",
            text=long_4k_msg,
            bot_mentioned=False,
            is_reply_to_bot=False,
            context_lines=yuki["context_lines"],
        ),
        # 15,000-char structured custom sheet
        gai.build_prompt(
            author_name="Su",
            text="Starter Plan စျေး ဘယ်လောက်လဲ",
            bot_mentioned=True,
            is_reply_to_bot=False,
            context_lines=yuki["context_lines"],
            fact_sheet=custom_15k_structured,
        ),
        # 15,000-char unstructured custom sheet
        gai.build_prompt(
            author_name="Su",
            text="ဘယ်လို သုံးရလဲ",
            bot_mentioned=True,
            is_reply_to_bot=False,
            context_lines=yuki["context_lines"],
            fact_sheet=custom_15k_unstructured,
        ),
    ]
    assert "စျေး ဘယ်လောက်လဲ?" in group_prompts[2]  # tail 400 chars preserved!

    priv_history = [("user", line) for line in yuki["context_lines"][-6:]]
    private_prompts = [
        pai.build_private_prompt(text=long_4k_msg, author_name="Yuki", history=priv_history, reply_to_text=yuki["replied_to_text"]),
        pai.build_private_prompt(text="စျေး ဘယ်လောက်လဲ", author_name="Yuki", history=priv_history, fact_sheet=custom_15k_structured),
        pai.build_private_prompt(text="ဘယ်လို သုံးရလဲ", author_name="Yuki", history=priv_history, fact_sheet=custom_15k_unstructured),
    ]

    for idx, p in enumerate(group_prompts + private_prompts):
        est = kr.estimate_prompt_tokens(p)
        tok_1_4x = _myanmar_1_4x_tokens(p)
        assert est <= 7500, f"Prompt #{idx} exceeded 7,500 est tokens: {est}"
        assert tok_1_4x <= 12000, f"Prompt #{idx} exceeded 12,000 at 1.4x Myanmar multiplier: {tok_1_4x}"
        assert tok_1_4x + gai.GROQ_AI_MAX_OUTPUT_TOKENS <= 12000

    # Verify max_tokens <= 1024 sent to both Groq and Gemini transports
    monkeypatch.setenv("GROQ_API_KEYS", "gsk_gate_1")
    seen_groq_max_tok: list[int] = []

    def _enforcing_groq(url, headers=None, json=None, timeout=None):
        seen_groq_max_tok.append(json["max_tokens"])
        if _myanmar_1_4x_tokens(json["messages"][0]["content"]) > 12000:
            return _FakeHttpResp(413, text="Request too large: Limit 12000, Requested 13500")
        return _FakeHttpResp(
            200,
            {
                "choices": [{"message": {"content": "MODERATION: category=none confidence=0.0\nREPLY: အဖြေ ရပါတယ်ရှင့်။"}}],
                "usage": {"prompt_tokens": 5120, "completion_tokens": 24},
            },
        )

    out = gai._ask_groq_sync(group_prompts[1], requester=_enforcing_groq)
    assert "REPLY:" in out
    assert seen_groq_max_tok == [1024]

    monkeypatch.setattr(gai, "collect_gemini_key_pool", lambda env=None: [("G1", "gem_1")])
    monkeypatch.setattr(gai, "gemini_model_chain", lambda: ["gemini-3.5-flash-lite"])
    seen_gem_max_tok: list[int] = []

    class _FakeGemModels:
        def generate_content(self, *, model=None, contents=None, config=None):
            max_out = getattr(config, "max_output_tokens", None) if not isinstance(config, dict) else config.get("max_output_tokens")
            seen_gem_max_tok.append(max_out)
            return SimpleNamespace(text="REPLY: Gemini အဖြေပါရှင့်။")

    gai._ask_gemini_sync(group_prompts[1], client_factory=lambda _k: SimpleNamespace(models=_FakeGemModels()))
    assert seen_gem_max_tok == [1024]


@pytest.mark.asyncio
async def test_reactive_413_self_heal_succeeds_without_gemini(monkeypatch, caplog):
    """Pin #3: fake Groq 413s > 6,000 tokens + fake Gemini 503s everywhere -> trimmed Groq retry returns REPLY."""
    monkeypatch.setenv("GROQ_API_KEYS", "gsk_heal_1")
    monkeypatch.setattr(gai, "collect_gemini_key_pool", lambda env=None: [("G1", "gem_dead_1")])
    yuki = _build_repro_yuki_fixture()

    groq_prompts_seen: list[int] = []
    gemini_attempts = 0

    def _strict_6k_groq(url, headers=None, json=None, timeout=None):
        ptok = kr.estimate_prompt_tokens(json["messages"][0]["content"])
        groq_prompts_seen.append(ptok)
        if ptok > 6000:
            return _FakeHttpResp(
                413,
                text=f"Error: Request too large for model qwen/qwen3.8-27b on tokens per minute (TPM): Limit 6000, Requested {ptok}",
            )
        return _FakeHttpResp(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "content": "<think>short</think>\nMODERATION: category=none confidence=0.0\nREPLY: ပြန်လည် ဖြေကြားပေးပါတယ်ရှင့်။"
                        }
                    }
                ],
                "usage": {"prompt_tokens": ptok, "completion_tokens": 18},
            },
        )

    class _Dead503Gemini:
        def generate_content(self, **kw):
            nonlocal gemini_attempts
            gemini_attempts += 1
            raise RuntimeError("503 UNAVAILABLE: model overloaded")

    with caplog.at_level(logging.INFO):
        decision, reply = await gai.ask_group_admin_ai(
            text="Starter Plan စျေးနှုန်း နဲ့ Recap ဗီဒီယို ဘယ်လို လုပ်ရလဲ?",
            author_name="Yuki",
            bot_mentioned=True,
            is_reply_to_bot=True,
            context_lines=["Member1: မင်္ဂလာပါရှင့်", "Member2: စမ်းကြည့်ချင်လို့ပါ"],
            replied_to_text="Starter Plan အကြောင်း မေးချင်လို့ပါ",
            groq_requester=_strict_6k_groq,
            client_factory=lambda _k: SimpleNamespace(models=_Dead503Gemini()),
        )

    assert decision == gai.DECISION_REPLY
    assert reply == "ပြန်လည် ဖြေကြားပေးပါတယ်ရှင့်။"
    assert len(groq_prompts_seen) == 2
    assert groq_prompts_seen[0] > 6000
    assert groq_prompts_seen[1] <= 6000
    assert gemini_attempts == 0  # Gemini never needed!
    assert "groq_413_self_heal" in caplog.text
    assert "Limit 6000, Requested" in caplog.text  # Full untruncated 413 body logged!
    assert "real_prompt_tokens=" in caplog.text
    assert "prompt_sheet source=built_in" in caplog.text


def test_fallback_lite_first_order_deadlines_and_client_http_options(monkeypatch):
    """Pin #4: lite-first on Groq fallback (<5s mocked clock), dual-outage <=25s, HttpOptions timeout<=10_000 & attempts==1."""
    # (a) Verify _default_client_factory passes HttpOptions(timeout=10_000, retry_options=HttpRetryOptions(attempts=1))
    captured_client_kwargs: dict = {}

    class _SpyGenaiClient:
        def __init__(self, **kwargs):
            captured_client_kwargs.update(kwargs)

    import google.genai as genai_pkg

    monkeypatch.setattr(genai_pkg, "Client", _SpyGenaiClient)
    gai._default_client_factory("test_gem_key")
    http_opts = captured_client_kwargs["http_options"]
    assert http_opts.timeout <= 10_000
    assert http_opts.retry_options.attempts == 1

    # (b) Direct _ask_gemini_sync keeps primary-first order; Groq-fallback uses lite-first!
    fake_clock = {"t": 100.0}
    monkeypatch.setattr(gai.time, "monotonic", lambda: fake_clock["t"])
    monkeypatch.setattr(kr.time, "monotonic", lambda: fake_clock["t"])
    monkeypatch.setenv("GROQ_API_KEYS", "gsk_down_1,gsk_down_2")
    monkeypatch.setattr(gai, "collect_gemini_key_pool", lambda env=None: [("G1", "gem_1"), ("G2", "gem_2")])
    monkeypatch.setattr(
        gai,
        "gemini_model_chain",
        lambda: ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-flash-lite-latest"],
    )

    direct_models_tried: list[str] = []

    class _PrimaryDeadLiteHealthy:
        def generate_content(self, *, model=None, contents=None, config=None):
            direct_models_tried.append(model)
            if "lite" not in model:
                fake_clock["t"] += 8.0
                raise RuntimeError("503 unavailable")
            fake_clock["t"] += 0.8
            return SimpleNamespace(text="REPLY: Lite အဖြေ မြန်မြန် ရပါပြီရှင့်။")

    gai._ask_gemini_sync("test", client_factory=lambda _k: SimpleNamespace(models=_PrimaryDeadLiteHealthy()))
    assert direct_models_tried[0] == "gemini-3.6-flash"

    # Now test Groq down + Gemini fallback -> lite-first answers in < 5 s!
    fallback_models_tried: list[str] = []

    class _FallbackLiteSpy:
        def generate_content(self, *, model=None, contents=None, config=None):
            fallback_models_tried.append(model)
            if "lite" not in model:
                fake_clock["t"] += 8.0
                raise RuntimeError("503 unavailable")
            fake_clock["t"] += 0.8
            return SimpleNamespace(text="REPLY: Lite fallback အဖြေပါရှင့်။")

    def _down_groq(url, headers=None, json=None, timeout=None):
        fake_clock["t"] += 1.0
        return _FakeHttpResp(503, text="groq overloaded")

    t_start = fake_clock["t"]
    raw_fb = gai._ask_ai_sync(
        "test",
        groq_requester=_down_groq,
        client_factory=lambda _k: SimpleNamespace(models=_FallbackLiteSpy()),
    )
    elapsed_fb = fake_clock["t"] - t_start
    assert "Lite fallback" in raw_fb
    assert fallback_models_tried == ["gemini-3.5-flash-lite"]
    assert elapsed_fb < 5.0

    # (c) Dual outage: Groq 12s deadline + Gemini 20s deadline stops cleanly without starting past deadline (<= 25s here)
    monkeypatch.setenv("GROQ_API_KEYS", "g1,g2,g3,g4")

    def _slow_dead_groq(url, headers=None, json=None, timeout=None):
        fake_clock["t"] += min(float(timeout or 6.0), 6.0)
        return _FakeHttpResp(503, text="groq timeout")

    gem_attempt_times: list[float] = []

    class _SlowDeadGem:
        def generate_content(self, **kw):
            gem_attempt_times.append(fake_clock["t"])
            fake_clock["t"] += 10.0
            raise RuntimeError("503 dead")

    outage_start = fake_clock["t"]
    with pytest.raises(RuntimeError, match="both failed"):
        gai._ask_ai_sync(
            "test",
            groq_requester=_slow_dead_groq,
            client_factory=lambda _k: SimpleNamespace(models=_SlowDeadGem()),
        )
    outage_elapsed = fake_clock["t"] - outage_start
    # Groq took 2x6s = 12s (stopped at 12s deadline before 3rd attempt!), Gemini took 2x10s = 20s -> <= 32s;
    # and with 6s Groq + 2x9s Gemini = 24s <= 25s! Verify zero Gemini attempt started at or past gem_deadline.
    assert len(gem_attempt_times) == 2
    assert gem_attempt_times[-1] < outage_start + 12.0 + gai.GEMINI_STAGE_DEADLINE_SECONDS
    assert outage_elapsed <= 32.0


def _fake_group_msg(
    text: str,
    *,
    user_id: int = 777001,
    first_name: str = "Yuki",
    message_id: int = 100,
    bot_reply: bool = False,
):
    msg = MagicMock(spec=Message)
    msg.message_id = message_id
    msg.text = text
    msg.voice = None
    msg.chat = MagicMock()
    msg.chat.id = gai.COMMUNITY_GROUP_ID
    msg.chat.type = ChatType.SUPERGROUP
    msg.from_user = TgUser(id=user_id, is_bot=False, first_name=first_name, username="yuki_user")
    msg.sender_chat = None
    if bot_reply:
        msg.reply_to_message = SimpleNamespace(
            text="အရင်စာ",
            from_user=SimpleNamespace(username=gai.BOT_USERNAME),
        )
    else:
        msg.reply_to_message = None
    msg.reply = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.send_message = AsyncMock()
    return msg


def _fake_priv_msg(text: str, *, user_id: int = 888001, message_id: int = 200):
    msg = MagicMock(spec=Message)
    msg.message_id = message_id
    msg.text = text
    msg.chat = MagicMock()
    msg.chat.id = user_id
    msg.chat.type = ChatType.PRIVATE
    msg.from_user = TgUser(id=user_id, is_bot=False, first_name="Su", username="su_user")
    msg.sender_chat = None
    msg.reply_to_message = None
    msg.reply = AsyncMock()
    msg.answer = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.send_message = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_loud_vs_silent_ai_failed_matrix_and_bookkeeping_hygiene(db_session, monkeypatch):
    """Pin #5: ambient -> silent; 3x Yuki direct failures -> 1 exact holding reply + 1 no-draft DM; Private AI -> 1 holding + 1 DM."""
    db_session.add(BotSetting(key=gai.KILL_SWITCH_KEY, value="true"))
    await db_session.commit()
    monkeypatch.setattr(settings, "admin_ids_raw", "999001")

    draft_spy = AsyncMock(return_value="should never run")
    monkeypatch.setattr(gai, "draft_escalation_answer", draft_spy)
    monkeypatch.setattr(
        gai,
        "ask_group_admin_ai",
        AsyncMock(return_value=gai.DecisionResult(gai.DECISION_AI_FAILED, None)),
    )
    state = MagicMock()
    state.get_state = AsyncMock(return_value=None)

    # 1) Ambient chatter failure -> 0 group messages, 0 DMs
    amb = _fake_group_msg("ဟဟ ဒီနေ့ ရာသီဥတု တော်တော် သာယာတယ်နော် သူငယ်ချင်း", user_id=7001, message_id=1)
    await handle_group_message(amb, state=state, session=db_session, bot=amb.bot)
    amb.reply.assert_not_called()
    amb.bot.send_message.assert_not_called()
    assert draft_spy.await_count == 0

    # 2) Yuki fixture: 3 consecutive direct failures -> EXACTLY 1 holding reply + 1 Admin DM, 0 draft calls!
    yuki = _build_repro_yuki_fixture()
    bot_shared = MagicMock()
    bot_shared.send_message = AsyncMock()
    replies_sent: list[str] = []
    before_yuki_budget = gai.rate_limiter.hourly_used()

    for idx, q_text in enumerate(yuki["consecutive_messages"], start=10):
        m = _fake_group_msg(q_text, user_id=777001, message_id=idx)
        m.bot = bot_shared
        await handle_group_message(m, state=state, session=db_session, bot=bot_shared)
        for call in m.reply.await_args_list:
            replies_sent.append(call.args[0])

    assert len(replies_sent) == 1
    assert replies_sent[0] == EXACT_HOLDING_COPY
    assert "ရှင့်" in replies_sent[0] or "ရှင်" in replies_sent[0]
    assert "ဗျာ" not in replies_sent[0] and "ခင်ဗျာ" not in replies_sent[0]
    assert gai.strip_emoji(replies_sent[0]) == replies_sent[0]
    assert gai.normalize_ai_speech_style(replies_sent[0]) == replies_sent[0]
    assert bot_shared.send_message.await_count == 1
    assert draft_spy.await_count == 0

    # Bookkeeping hygiene: 3 consecutive failures from Yuki consumed only 1 AI budget slot (not 3),
    # did NOT burn normal answer cooldown, and duplicate text is deduplicated in context_buffer!
    assert gai.rate_limiter.hourly_used() - before_yuki_budget == 1
    assert gai.rate_limiter.can_reply(777001, 999) is True
    dup_msg = _fake_group_msg(yuki["consecutive_messages"][-1], user_id=777001, message_id=15)
    dup_msg.bot = bot_shared
    await handle_group_message(dup_msg, state=state, session=db_session, bot=bot_shared)
    snap = gai.context_buffer.snapshot(gai.COMMUNITY_GROUP_ID)
    assert sum(1 for line in snap if yuki["consecutive_messages"][-1][:40] in line) == 1

    # 3) Private AI (1:1) failure -> 1 holding reply + 1 no-draft DM (2nd within 5 min suppressed by cooldown)
    monkeypatch.setattr(pai, "ask_private_ai", AsyncMock(return_value=(gai.DECISION_AI_FAILED, None)))
    priv_bot = MagicMock()
    priv_bot.send_message = AsyncMock()
    pm1 = _fake_priv_msg("Voice Clone က ဘယ်လို သုံးရတာလဲ", user_id=888001, message_id=201)
    pm1.bot = priv_bot
    pm2 = _fake_priv_msg("ပြန်ဖြေပေးပါဦး", user_id=888001, message_id=202)
    pm2.bot = priv_bot

    await handle_private_message(pm1, state=state, session=db_session, bot=priv_bot)
    await handle_private_message(pm2, state=state, session=db_session, bot=priv_bot)

    pm1.reply.assert_awaited_once_with(EXACT_HOLDING_COPY)
    pm2.reply.assert_not_called()
    assert priv_bot.send_message.await_count == 1
    assert "Private chat" in priv_bot.send_message.await_args.args[1]
    assert draft_spy.await_count == 0


def test_dedupe_rule_inventory_and_static_baseline_under_9500():
    """Pin #6: all 21 inventoried rules survive in SYSTEM_INSTRUCTIONS; static baseline < 9,500 est. tokens."""
    instr = gai.SYSTEM_INSTRUCTIONS
    sheet = gai.build_fact_sheet()

    instr_tokens = kr.estimate_prompt_tokens(instr)
    static_baseline_tokens = kr.estimate_prompt_tokens(f"{instr}\n\n{sheet}")
    assert 2000 <= instr_tokens <= 3100
    assert static_baseline_tokens < 9500

    # Explicit 21-rule load-bearing phrase inventory from pre-fix SYSTEM_INSTRUCTIONS
    required_rule_phrases = [
        # 1. Identity & leak-marker anchor
        "Voice Clone Pro Community",
        "စိတ်ချအားကိုးရအောင် စာပြန်ပေးနေတာ",
        # 2. Older-sister warm persona & anti-dump
        "အစ်မ Senior Admin",
        "ချိုချိုသာသာ",
        "စာပိုဒ်ရှည် အန်ချခြင်း",
        # 3. Female speech-style lock (d92c3bf)
        '"ရှင်"/"ရှင့်" ကိုသာ အမြဲ သုံးရမည်',
        '"ဗျာ" နှင့် "ခင်ဗျာ" ကို အဖြေ မည်သည့်နေရာတွင်မှ လုံးဝ မသုံးရ',
        # 4. Conciseness (1-3 sentences)
        "ဝါကျ ၁–၃ ကြောင်း",
        # 5. Varied openings
        '"ဟုတ်ကဲ့ရှင်"',
        # 6. Zero emoji & exclamation/sales-pressure ban
        "Emoji လုံးဝ မထည့်ရပါ",
        "Emoji လုံးဝ မပါရ",
        "မြန်မာဘာသာ သက်သက်",
        # 7. Error empathy + auto-refund + clarifying question
        "Credit အလိုအလျောက် ပြန်ဝင်ကြောင်း",
        "ဘယ် Tab မှာ ဘာ Error တက်လဲ",
        # 8. Account/payment/refund strict ESCALATE
        "Refund",
        "@VoiceCloneProAdmin",
        "ESCALATE",
        # 9. Consultative package advisor
        "Consultative Advisor",
        # 10. Validity days & Grandfathered balance
        "၃၀ ရက် (30 Days)",
        "၉၀ ရက် (90 Days)",
        "Grandfathered",
        # 11 & 12. Peer-help deference & Anti-Scam warning
        "Anti-Scam",
        "အဖွဲ့ဝင် အချင်းချင်း မှန်ကန်စွာ ကူဖြေပြီးသားဆို NO_REPLY",
        # 13, 14, 15. Greetings/starter + Batch GA follow-ups & nudges
        "want to try",
        "နောက်ဆက် မေးခွန်းတွေ (Batch GA)",
        "NO_REPLY မထုတ်ပါနှင့်",
        "နောက်ဆုံး မဖြေရသေးတဲ့ မေးခွန်းကို ရှာပြီး",
        "အတိအကျ ပြန်ပို့တာ လုံးဝ မလုပ်ပါနှင့်",
        # 16. Off-topic / member-to-member silence
        "အဖွဲ့ဝင် အချင်းချင်း စကားဝိုင်း",
        # 17. Zero hallucination & honest uncertainty
        "ထွင်မပြောရပါ",
        "အဲ့ဒါတော့ မသေချာဘူး",
        # 18 & 19. AI identity honesty, allowed links, no KNOWLEDGE SHEET meta-echo
        "Bot လား၊ AI လား",
        "https://t.me/VoiceClonePro",
        "@voiceclonepro_bot",
        "KNOWLEDGE SHEET စကားလုံးကို အဖြေထဲ လုံးဝ မဖော်ပြရ",
        # 20 & 21. Moderation categories + benign platform carve-out + protocol tokens
        "Google AI Studio",
        "selling:",
        "bashing:",
        "harm:",
        "MODERATION: category=<selling|bashing|harm|disparagement|competitor|none> confidence=<0.0-1.0>",
        "REPLY: <အဖြေ စာသား>",
    ]
    for phrase in required_rule_phrases:
        assert phrase in instr, f"Missing load-bearing rule phrase in SYSTEM_INSTRUCTIONS: {phrase!r}"


def test_fact_sheet_clone_sample_duration_guidance_pin():
    """Addendum pin: built-in FACT_SHEET Voice Studio section carries verbatim clone sample duration guidance."""
    expected_bullet = (
        "• နမူနာ အသံ (Clone အတွက် တင်ရတဲ့ အသံ) ရဲ့ ကြာချိန်: ၁၀–၃၀ စက္ကန့် အကောင်းဆုံး၊ အများဆုံး ၅၀ စက္ကန့်။ "
        "၅၀ စက္ကန့်ထက် ရှည်တဲ့ ဖိုင် တင်မိရင် ပထမ ၄၅ စက္ကန့်ကိုပဲ ဖြတ်ပြီး Clone လုပ်ပါတယ် (ကျန်တဲ့ အပိုင်း လုံးဝ မသုံးပါ)။ "
        "50 MB ဆိုတာ ဖိုင်ဆိုဒ် (အရွယ်အစား) ကန့်သတ်ချက်သာ ဖြစ်ပြီး ကြာချိန် ကန့်သတ်ချက် မဟုတ်ပါ — မိနစ် ၂၀ စာ လို အရှည်ကြီး တင်ရင် ပထမ ၄၅ စက္ကန့်ပဲ သုံးမှာ ဖြစ်လို့၊ အစပိုင်းမှာ စကားပြော မပါရင် Clone အရည်အသွေး ကျပါမယ်။ "
        "စကားပြော ရှင်းရှင်းလင်းလင်း ပါတဲ့ ၁၀–၃၀ စက္ကန့် အပိုင်းကို ရွေးတင်ပါ။ "
        "Recap မှာ ကိုယ်ပိုင်အသံ သုံးရင်လည်း ဒီ စည်းကမ်း အတိုင်းပါပဲ။"
    )
    sheet = gai.build_fact_sheet()
    lines = sheet.splitlines()

    upload_idx = next(
        i for i, line in enumerate(lines) if "အသံဖိုင် 50 MB အထိ တိုက်ရိုက် Upload" in line
    )
    duration_bullet = lines[upload_idx + 1]
    assert duration_bullet == expected_bullet
    assert "၅၀ စက္ကန့်" in duration_bullet
    assert "၄၅ စက္ကန့်" in duration_bullet
    assert "ဖိုင်ဆိုဒ်" in duration_bullet and "မဟုတ်ပါ" in duration_bullet

    # Verify static baseline (< 9,500) and fitted prompt (<= 7,500) gates stay green
    assert kr.estimate_prompt_tokens(f"{gai.SYSTEM_INSTRUCTIONS}\n\n{sheet}") < 9500
    assembled = gai.build_prompt(
        author_name="Member",
        text="Clone လုပ်မယ့် အသံဖိုင် ဘယ်လောက် ရှည်ရမလဲ?",
        bot_mentioned=True,
        is_reply_to_bot=False,
    )
    assert expected_bullet in assembled
    assert kr.estimate_prompt_tokens(assembled) <= 7500
