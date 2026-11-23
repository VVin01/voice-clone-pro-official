"""Batch BX (2026-11-07): the firsthand-walkthrough fix bundle.

Built from the 2026-11-07 00:18–01:19 FREE + admin walkthroughs
(owner order: "အကုန်လုပ် သေးသေးကြီးကြီး တစ်ခါတည်းအပြီးလုပ်" — every
finding, small and large, in ONE batch):

1. Bot guide/help text tells the truth (daily MINUTE caps, TTS caps,
   FREE recap limits, BU sample guidance) — the surfaces disagreed.
2. The 🏠 menu button answers immediately and can no longer look
   dead on old messages (not-modified → fresh menu message).
3. TTS bot delivery names its engine + voice; History shows friendly
   voice names instead of raw engine ids ("Charon",
   "my-MM-ThihaNeural").
4. Recap Step 3: all three engine labels always visible; the Edge
   engine's persona grid is honestly labelled as a base-voice pick;
   the pre-profile default engine is the FREE-safe "gemini".
5. The entry gate boots in a neutral "checking" state — no more
   join-gate flash carrying 0 balances.
6. Studio sliders: taller touch boxes, pan-x, bigger thumbs, wider
   row rhythm (CSS-only; the decisive check is a real phone).
7. Voice Clone speed labels use the same % language as TTS/Recap.
8. NEW: bot private-chat AI — plain-text 1:1 messages get the Group
   AI treatment (fact-sheet answers, sensitive → admins, silence on
   failure) on a separate limiter, default ON.
A1. Purchased character-limit save regression pins (5000/1000
    save→read-back verbatim; no transform may creep into the path).
A2. Own-voice recap rows show the own-voice label in the admin jobs
    list and user History (they showed the default "komin" slug).
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from aiogram.types import User as TgUser

from app.config import settings
from app.models.schema import BotSetting
from app.services import group_ai_admin as gai
from app.services.i18n import load_locales, t

ROOT = Path(__file__).resolve().parents[2]

load_locales()


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ===========================================================================
# Item 1 — guide / help / sample copy truth
# ===========================================================================

def _render_guide(lang: str) -> str:
    return t(
        "help_settings_guide",
        lang,
        free_min="3",
        free_sec=180,
        char_free="1,000",
        char_purchased="5,000",
        # Batch BZ: the FREE voice day renders as a clause built from
        # the cap (capped phrase here, with the value an admin set).
        voice_daily_free_clause=t(
            "guide_voice_daily_free_capped", lang, minutes=5
        ),
        voice_daily_paid=30,
        tts_daily_free=15,
        tts_daily_paid=60,
        recap_free_min=2,
        vip_recap_min=10,
        retention_hours=24,
        referral_seconds=120,
        referral_cap_min=40,
    )


def test_guide_renders_both_languages_with_real_limits():
    for lang in ("my", "en"):
        out = _render_guide(lang)
        assert "{" not in out, f"{lang} guide has an unfilled placeholder"
        assert "120" in out  # referral seconds (already true pre-BX)
        # The per-day MINUTE caps are named now (Item 1a).
        for needle in ("5", "30", "15", "60"):
            assert needle in out


def test_guide_no_longer_names_the_legacy_daily_job_count():
    # Round 13 Part K retired render_help_settings_content; locale templates still exclude {daily_limit}.
    for lang in ("my", "en"):
        template = t("help_settings_guide", lang)
        assert "{daily_limit}" not in template


def test_guide_states_free_recap_limits_and_sample_guidance():
    # Limits round + Round 13: help_settings_guide states free recap limits & watermark.
    my = _render_guide("my")
    assert "Watermark" in my
    en = _render_guide("en")
    assert "watermark" in en


def test_help_desc_points_at_the_full_limits():
    assert "အကူအညီ & ဆက်တင်" in t("help_desc", "my")
    assert "Help & Settings" in t("help_desc", "en")


def test_clone_ref_prompt_uses_bu_sample_truth():
    for lang in ("my", "en"):
        prompt = t("clone_ref_prompt", lang)
        assert "50" in prompt and "45" in prompt
        assert "၃–၃၀" not in prompt and "3–30 seconds" not in prompt


def test_webapp_custom_ref_desc_uses_bu_sample_truth():
    i18n_js = _read("app/webapp/static/js/i18n.js")
    assert "voice_ref_trim_notice" in i18n_js
    assert "50" in i18n_js and "45" in i18n_js
    assert "၃–၃၀ စက္ကန့်၊ WAV/MP3" not in i18n_js


def test_stale_referral_comments_updated():
    # Limits round (2026-11-23): referral rewards 60s Voice Clone per friend, uncapped.
    from app.services import ledger as ledger_mod

    assert ledger_mod.REFERRAL_REWARD_SECONDS == 60
    assert ledger_mod.MAX_REFERRALS_REWARDED is None


def test_referral_message_itself_unchanged():
    # Recon finding: the live referral_desc was already correct —
    # Item 1 must NOT have churned it.
    assert "{referral_seconds}" in t("referral_desc", "my")
    assert "{referral_seconds}" in t("referral_desc", "en")


# ===========================================================================
# Item 2 — menu button cannot look dead
# ===========================================================================

def test_safe_edit_message_reports_not_modified_on_request():
    from app.ui.helpers import NOT_MODIFIED, safe_edit_message

    async def _run():
        msg = MagicMock(spec=Message)
        for attr in ("photo", "document", "video", "animation", "audio", "voice", "caption"):
            setattr(msg, attr, None)
        msg.edit_text = AsyncMock(
            side_effect=TelegramBadRequest(
                method=MagicMock(), message="Bad Request: message is not modified"
            )
        )
        cb = MagicMock()
        cb.message = msg
        cb.from_user = TgUser(id=424242, is_bot=False, first_name="T")
        cb.bot = MagicMock()
        cb.bot.send_message = AsyncMock()
        result = await safe_edit_message(cb, "Same", report_unmodified=True)
        assert result is NOT_MODIFIED
        cb.bot.send_message.assert_not_called()
        # Default behaviour is unchanged: the original message returns.
        result2 = await safe_edit_message(cb, "Same")
        assert result2 is msg

    import asyncio

    asyncio.run(_run())


def test_safe_edit_message_other_callers_unaffected():
    src = _read("app/ui/helpers.py")
    assert "report_unmodified" in src
    # The kwarg is popped, never forwarded to Telegram calls.
    assert 'kwargs.pop("report_unmodified", False)' in src


def _menu_callback(*, not_modified: bool):
    cb = MagicMock()
    cb.from_user = TgUser(id=987001, is_bot=False, first_name="Menu")
    cb.answer = AsyncMock()
    msg = MagicMock(spec=Message)
    for attr in ("photo", "document", "video", "animation", "audio", "voice", "caption"):
        setattr(msg, attr, None)
    if not_modified:
        msg.edit_text = AsyncMock(
            side_effect=TelegramBadRequest(
                method=MagicMock(), message="Bad Request: message is not modified"
            )
        )
    else:
        msg.edit_text = AsyncMock(return_value=msg)
    cb.message = msg
    cb.bot = MagicMock()
    cb.bot.send_message = AsyncMock()
    return cb


def test_active_start_menu_callbacks_have_handlers():
    # Round 13 Part K rebuilt bot /start to 3 buttons (Mini App, Refer, Language) and retired
    # legacy bot-chat admin/history/samples sub-menus. Verify every active /start callback has a handler.
    from app.ui.keyboards import main_menu_kb

    kb = main_menu_kb(lang="my")
    callbacks = [
        btn.callback_data
        for row in kb.inline_keyboard
        for btn in row
        if btn.callback_data
    ]
    assert callbacks  # menu:refer and menu:language
    handler_src = ""
    for path in (ROOT / "app/handlers").glob("*.py"):
        handler_src += path.read_text(encoding="utf-8") + "\n"
    equals = set(re.findall(r'F\.data == "([^"]+)"', handler_src))
    starts = set(re.findall(r'F\.data\.startswith\("([^"]+)"\)', handler_src))
    for cb in callbacks:
        assert cb in equals or any(cb.startswith(s) for s in starts), cb


# ===========================================================================
# Item 3 — TTS naming (delivery caption + History display fields)
# ===========================================================================

def test_tts_voice_display_name_maps_engine_ids_to_friendly_names():
    from app.services.voice_display import tts_voice_display_name

    assert tts_voice_display_name("edge", "my-MM-ThihaNeural") == "ကိုသီဟ"
    assert tts_voice_display_name("edge", "my-MM-NilarNeural") == "မနီလာ"
    gemini_name = tts_voice_display_name("gemini", "Charon")
    assert gemini_name and gemini_name != "Charon"  # friendly Burmese name
    # Unknown values degrade to the raw value, never blank.
    assert tts_voice_display_name("gemini", "SomeNewVoice") == "SomeNewVoice"
    assert tts_voice_display_name("edge", "other-Neural") == "other-Neural"


def test_tts_engine_display_name():
    from app.services.voice_display import tts_engine_display_name

    assert tts_engine_display_name("gemini") == "Gemini"
    assert tts_engine_display_name("edge") == "Edge"
    assert tts_engine_display_name("edge_tts") == "Edge"


def test_tts_delivery_caption_key_renders_both_languages():
    # Part F compact delivery caption takes engine, voice, duration, job_id, retention_hours.
    my = t("tts_delivery_caption", "my", engine="Gemini", voice="ကိုသီဟ", duration="00:30", job_id="t1", retention_hours=24)
    assert "Gemini" in my and "ကိုသီဟ" in my and "{" not in my
    en = t("tts_delivery_caption", "en", engine="Edge", voice="Ko Thiha", duration="00:30", job_id="t1", retention_hours=24)
    assert "Edge" in en and "Ko Thiha" in en and "{" not in en
    src = _read("app/services/tts_service.py")
    assert 't("tts_delivery_caption"' in src or '"tts_delivery_caption"' in src
    assert "tts_voice_display_name(getattr(job, \"engine\", None), getattr(job, \"preset_voice\", None))" in src


def test_job_display_names_helper():
    from app.webapp.api import _job_display_names

    tts_job = SimpleNamespace(
        job_type="TTS", engine="edge",
        preset_voice="my-MM-ThihaNeural", recap_ref_audio_path=None,
    )
    voice, engine = _job_display_names(tts_job, is_recap=False, is_tts=True)
    assert voice == "ကိုသီဟ" and engine == "Edge"

    own = SimpleNamespace(
        job_type="RECAP", engine="recap",
        preset_voice="komin", recap_ref_audio_path="/tmp/ref.wav",
    )
    voice, engine = _job_display_names(own, is_recap=True, is_tts=False)
    assert voice == "🎙️ ကိုယ်ပိုင်အသံ" and engine is None

    plain = SimpleNamespace(
        job_type="RECAP", engine="recap",
        preset_voice="komin", recap_ref_audio_path=None,
    )
    assert _job_display_names(plain, is_recap=True, is_tts=False) == (None, None)


def test_serializers_carry_display_names_and_keep_raw_preset_voice():
    src = _read("app/webapp/api.py")
    assert src.count('"voice_display": _job_display_names(') == 3
    # The raw value is still serialized (admin/debug surfaces use it).
    assert '"preset_voice": j.preset_voice or ("Thiha" if is_recap else "Standard")' in src


def test_frontend_history_and_admin_rows_prefer_voice_display():
    js = _read("app/webapp/static/js/app.js")
    assert 'escapeHtml(j.voice_display || j.preset_voice || "")' in js  # user History
    assert 'escapeHtml(j.voice_display || j.preset_voice)' in js  # admin queue tag
    assert 'escapeHtml(j.voice_display || j.preset_voice || j.engine)' in js  # admin history


# ===========================================================================
# Item 4 — Recap Step-3 labels + Edge honesty
# ===========================================================================

def test_engine_picker_css_shows_all_labels():
    css = _read("app/webapp/static/css/style.css")
    assert "#recap-engine-picker .capsule-seg .seg-full { display: inline; }" in css
    assert "#recap-engine-picker .capsule-seg .seg-short { display: none; }" in css
    assert "#recap-engine-picker .pill-btn.capsule-seg.active { flex-grow: 1; }" in css
    # The shared capsule swap for other pickers is untouched.
    assert ".seg-capsule .capsule-seg .seg-full { display: none; }" in css


def test_edge_voice_picker_and_wiring():
    # Round 13 + Part D: Edge TTS has its own 8-voice picker (#recap-edge-voice-wrap / #recap-edge-voice-select)
    # separate from the VoxCPM persona area, synced by syncRecapEngineAreas().
    index = _read("app/webapp/static/index.html")
    assert 'id="recap-edge-voice-wrap"' in index
    assert 'id="recap-edge-voice-select"' in index
    js = _read("app/webapp/static/js/app.js")
    sync_start = js.index("function syncRecapEngineAreas()")
    sync_body = js[sync_start:js.index("function refreshRecapEngineLock()", sync_start)]
    assert 'const eng = state.recapTtsEngine || "voxcpm";' in sync_body
    assert 'recap-edge-voice-wrap' in sync_body


def test_step4_summary_resolves_edge_voice():
    js = _read("app/webapp/static/js/app.js")
    start = js.index("function refreshRecapStep4Choices()")
    end = js.index("\nfunction ", start + 1)
    body = js[start:end]
    assert 'eng === "edge"' in body
    assert "recap-edge-voice-select" in body or "recapEdgeVoice" in body


def test_default_recap_engine_is_free_safe():
    js = _read("app/webapp/static/js/app.js")
    assert 'recapTtsEngine: "gemini",' in js
    assert 'recapTtsEngine: "voxcpm",' not in js


# ===========================================================================
# Item 5 — entry gate boots neutral
# ===========================================================================

def test_gate_checking_mode_exists_and_is_entered_first():
    js = _read("app/webapp/static/js/app.js")
    assert 'mode === "checking"' in js
    assert 'setEntryGateMode("checking")' in js.split("async function initEntryGate()")[1][:400]


def test_gate_static_html_ships_in_checking_state():
    index = _read("app/webapp/static/index.html")
    assert '<h3 id="gate-lock-title" data-i18n="gate_lock_title"></h3>' in index
    assert 'class="gate-banner-chips gate-lock-chips hidden"' in index
    assert 'class="gate-lock-actions hidden"' in index
    # Header chips show the neutral placeholder, not fake zero balances.
    assert "🎙️ —" in index and "🎬 —" in index
    assert "🎙️ 0.0m" not in index and "🎬 0c" not in index


def test_gate_lock_semantics_untouched():
    js = _read("app/webapp/static/js/app.js")
    # applyEntryGateState / verdict handling still the only unlock path.
    assert "function applyEntryGateVerdict" in js
    assert 'setEntryGateMode("banned", data)' in js


# ===========================================================================
# Item 6 — slider touch geometry (CSS pins)
# ===========================================================================

def test_slider_css_touch_geometry():
    css = _read("app/webapp/static/css/style.css")
    block = css.split(".studio-slider {")[1].split("}")[0]
    assert "height: 40px" in block
    assert "touch-action: pan-x" in block
    assert "width: 26px; height: 26px; margin-top: -10.5px" in css
    assert ".editor-controls { display: flex; flex-direction: column; gap: var(--s4); }" in css
    assert "gap: var(--s2); padding-block: var(--s1);" in css


# ===========================================================================
# Item 7 — speed labels (% language everywhere)
# ===========================================================================

def test_voice_speed_labels_use_percent_language():
    i18n_js = _read("app/webapp/static/js/i18n.js")
    for needle in (
        'speed_m6: "-6% နှေး"', 'speed_0: "+0% ပုံမှန်"', 'speed_12_capsule: "+12% အကြံပြု"',
        'speed_20: "+20% မြန်"', 'speed_25: "+25% အထူးမြန်"',
        'speed_m6: "-6% Slow"', 'speed_0: "+0% Normal"', 'speed_12_capsule: "+12% Top pick"',
        'speed_20: "+20% Fast"', 'speed_25: "+25% Ultra-fast"',
    ):
        assert needle in i18n_js
    index = _read("app/webapp/static/index.html")
    for s in ("-6%", "+0%", "+12%", "+20%", "+25%"):
        assert f'<span class="seg-short">{s}</span>' in index
        assert f'data-speed="{s}"' in index
    for tok in ("0.8x", "1.0x", "1.2x"):
        assert f'"{tok}"' not in i18n_js


# ===========================================================================
# A1 — character-limit save/read-back regression pins
# ===========================================================================

@pytest.mark.asyncio
async def test_char_limit_save_readback_verbatim_5000_and_1000(db_session):
    """Save 5000 (purchased) + 1000 (free) through the real settings
    path and read them back through the real GET serializer — the
    values must survive byte-exact. (BX A1: no transform exists in
    the path; these pins keep it that way.)"""
    from app.services.dynamic_settings import set_dynamic_setting
    from app.webapp import api as api_mod

    await set_dynamic_setting(db_session, "character_limit_purchased", 5000)
    await set_dynamic_setting(db_session, "character_limit_free", 1000)
    await db_session.commit()

    admin = SimpleNamespace(id=1, is_admin=True)
    payload = await api_mod.get_admin_settings(admin, db_session)
    assert payload["character_limit_purchased"] == 5000
    assert payload["character_limit_free"] == 1000
    assert settings.character_limit_purchased == 5000
    assert settings.character_limit_free == 1000


def test_char_limit_validator_passes_values_verbatim():
    from app.webapp.api import _validate_admin_settings_updates

    cleaned = _validate_admin_settings_updates(
        {"character_limit_free": 1000, "character_limit_purchased": 5000}
    )
    assert cleaned == {"character_limit_free": 1000, "character_limit_purchased": 5000}


def test_no_scaling_factor_in_settings_save_path():
    """Source pin for A1: the save/read path contains no scaling
    arithmetic on the way in or out."""
    api_src = _read("app/webapp/api.py")
    validator = api_src.split("def _validate_admin_settings_updates")[1].split("\ndef ")[0]
    assert "0.75" not in validator and "* 3" not in validator
    ds_src = _read("app/services/dynamic_settings.py")
    assert "0.75" not in ds_src
    app_js = _read("app/webapp/static/js/app.js")
    assert "0.75" not in app_js


# ===========================================================================
# Item 8 — private-chat AI
# ===========================================================================

from app.handlers.group_admin import handle_private_message
from app.services import private_ai as pai


def test_fact_sheet_referral_matches_limits_round():
    # Limits round + Part H: referral reward is 60s (1 min) Voice Clone per friend, uncapped.
    sheet = gai.build_fact_sheet()
    assert "60 စက္ကန့်" in sheet
    assert "Voice Clone 1 မိနစ်" in sheet
    assert "အကန့်အသတ် မရှိ" in sheet


def test_private_limiter_is_a_separate_instance():
    assert pai.private_rate_limiter is not gai.rate_limiter
    assert isinstance(pai.private_rate_limiter, gai.GroupReplyRateLimiter)
    # The private module never references the group's persistence
    # machinery — its limiter is in-memory only by construction.
    src = _read("app/services/private_ai.py")
    assert "LIMITER_STATE_KEY" not in src
    assert "snapshot_state" not in src and "restore_state" not in src


def test_private_prefilter_matrix():
    base = dict(
        chat_type="private", text="Recap ဘယ်လိုလုပ်ရလဲ",
        from_user_id=555001, from_user_is_bot=False, sender_chat_id=None,
    )
    assert pai.prefilter_private_reason(**base) is None
    assert pai.prefilter_private_reason(**{**base, "chat_type": "supergroup"}) == "not_a_private_chat"
    assert pai.prefilter_private_reason(**{**base, "text": "/start"}) == "command"
    assert pai.prefilter_private_reason(**{**base, "from_user_is_bot": True}) == "bot_or_anonymous_author"
    assert pai.prefilter_private_reason(**{**base, "sender_chat_id": -1001}) == "sender_chat_post"
    assert pai.prefilter_private_reason(**{**base, "text": None}) == "no_text"
    assert pai.prefilter_private_reason(**{**base, "text": "x"}) == "too_short"


def test_private_prompt_framing():
    prompt = pai.build_private_prompt(text="စျေးနှုန်း သိချင်ပါတယ်", author_name="Aung")
    assert "Private Chat" in prompt
    assert "Group ထဲက စာ" not in prompt  # no group context block
    assert "စျေးနှုန်း သိချင်ပါတယ်" in prompt
    assert "KNOWLEDGE SHEET" in prompt


@pytest.mark.asyncio
async def test_private_kill_switch_defaults_on(db_session):
    assert await pai.is_private_ai_enabled(db_session) is True
    db_session.add(BotSetting(key="private_ai_enabled", value="false"))
    await db_session.commit()
    assert await pai.is_private_ai_enabled(db_session) is False


@pytest.mark.asyncio
async def test_private_limits_defaults_and_overrides(db_session):
    limits = await pai.get_private_limits(db_session)
    assert (limits.user_cooldown_seconds, limits.hourly_budget, limits.daily_budget) == (60, 40, 300)
    db_session.add(BotSetting(key="private_ai_hourly_budget", value="7"))
    await db_session.commit()
    limits = await pai.get_private_limits(db_session)
    assert limits.hourly_budget == 7


def _fake_private_message(text, *, user_id=555001, message_id=10):
    msg = MagicMock(spec=Message)
    msg.message_id = message_id
    msg.text = text
    msg.chat = MagicMock()
    msg.chat.id = user_id
    msg.chat.type = ChatType.PRIVATE
    msg.from_user = TgUser(id=user_id, is_bot=False, first_name="Aung", username="aung")
    msg.sender_chat = None
    msg.reply_to_message = None
    msg.reply = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.send_message = AsyncMock()
    return msg


def _fake_state(current=None):
    state = MagicMock()
    state.get_state = AsyncMock(return_value=current)
    return state


@pytest.fixture(autouse=True)
def _fresh_private_limiter(monkeypatch):
    monkeypatch.setattr(pai, "private_rate_limiter", gai.GroupReplyRateLimiter())


@pytest.mark.asyncio
async def test_private_greeting_gets_the_welcome_reply(db_session):
    msg = _fake_private_message("မင်္ဂလာပါ")
    await handle_private_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    msg.reply.assert_awaited_once()
    assert msg.reply.await_args.args[0] == pai.PRIVATE_WELCOME_TEXT


@pytest.mark.asyncio
async def test_private_question_gets_ai_reply_stripped_of_emoji(db_session, monkeypatch):
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "😀 Bot ကို ဖွင့်ပြီး စလို့ ရပါတယ်။"))
    monkeypatch.setattr(pai, "ask_private_ai", ai)
    msg = _fake_private_message("Recap က ဘယ်လို လုပ်ရတာလဲ")
    await handle_private_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    ai.assert_awaited_once()
    msg.reply.assert_awaited_once()
    assert "😀" not in msg.reply.await_args.args[0]
    assert "စလို့ ရပါတယ်" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_private_sensitive_topic_defers_and_escalates_without_ai(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", "777001")
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "မဖြေသင့်တဲ့ အဖြေ"))
    monkeypatch.setattr(pai, "ask_private_ai", ai)
    draft = AsyncMock(return_value=None)
    monkeypatch.setattr(gai, "draft_escalation_answer", draft)
    msg = _fake_private_message("ငွေသွင်းပြီးပြီ၊ မိနစ် မဝင်သေး ဘူး ခင်ဗျာ", message_id=77)
    await handle_private_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    ai.assert_not_called()  # the sensitive hard rule fires before any AI answer
    msg.reply.assert_awaited_once()
    deferral = msg.reply.await_args.args[0]
    assert deferral == t("private_ai_deferral_sensitive", "my")
    # The escalation DM went to the admins WITHOUT a group link.
    assert msg.bot.send_message.await_count >= 1
    dm_texts = [c.args[1] for c in msg.bot.send_message.await_args_list]
    assert any("Private chat" in x for x in dm_texts)
    assert all("t.me/c/" not in x for x in dm_texts)
    # The private limiter took the deferral; the group's did not move.
    assert gai.rate_limiter._last_deferral_by_user == {}
    assert pai.private_rate_limiter._last_deferral_by_user != {}


@pytest.mark.asyncio
async def test_private_ai_failure_is_silence(db_session, monkeypatch):
    ai = AsyncMock(return_value=(gai.DECISION_AI_FAILED, None))
    monkeypatch.setattr(pai, "ask_private_ai", ai)
    msg = _fake_private_message("Voice Clone ဆိုတာ ဘာလဲ")
    await handle_private_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    msg.reply.assert_not_called()


@pytest.mark.asyncio
async def test_private_kill_switch_off_is_silence(db_session, monkeypatch):
    db_session.add(BotSetting(key="private_ai_enabled", value="false"))
    await db_session.commit()
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "အဖြေ"))
    monkeypatch.setattr(pai, "ask_private_ai", ai)
    msg = _fake_private_message("ဘယ်လို စသုံးရမလဲ ခင်ဗျာ")
    await handle_private_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    msg.reply.assert_not_called()
    ai.assert_not_called()


@pytest.mark.asyncio
async def test_private_cooldown_blocks_the_second_question(db_session, monkeypatch):
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "အဖြေ တစ်ခု"))
    monkeypatch.setattr(pai, "ask_private_ai", ai)
    first = _fake_private_message("Recap က ဘယ်လို လုပ်ရတာလဲ", message_id=1)
    await handle_private_message(first, state=_fake_state(), session=db_session, bot=first.bot)
    first.reply.assert_awaited_once()
    second = _fake_private_message("ဘယ်လို စသုံးရမလဲ ခင်ဗျာ", message_id=2)
    await handle_private_message(second, state=_fake_state(), session=db_session, bot=second.bot)
    second.reply.assert_not_called()
    ai.assert_awaited_once()  # the cooldown stopped it before the AI


@pytest.mark.asyncio
async def test_private_message_inside_fsm_flow_is_not_hijacked(db_session, monkeypatch):
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "အဖြေ"))
    monkeypatch.setattr(pai, "ask_private_ai", ai)
    msg = _fake_private_message("ဒါက clone စာသား ဖြစ်ပါတယ်")
    await handle_private_message(
        msg, state=_fake_state("CloneStates:waiting_text"), session=db_session, bot=msg.bot
    )
    msg.reply.assert_not_called()
    ai.assert_not_called()


@pytest.mark.asyncio
async def test_private_command_is_not_answered(db_session, monkeypatch):
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "အဖြေ"))
    monkeypatch.setattr(pai, "ask_private_ai", ai)
    msg = _fake_private_message("/start")
    await handle_private_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    msg.reply.assert_not_called()
    ai.assert_not_called()


def test_build_escalation_dm_private_variant_has_no_group_link():
    dm = gai.build_escalation_dm(
        author_name="Aung", author_username="aung", author_id=555001,
        text="ငွေ မဝင်သေးဘူး", chat_id=555001, message_id=9,
        reason="sensitive", dm_link=None,
    )
    assert "Private chat" in dm
    assert "t.me/c/" not in dm
    # Default (group) behaviour is byte-identical to before.
    dm_group = gai.build_escalation_dm(
        author_name="Aung", author_username="aung", author_id=555001,
        text="ငွေ မဝင်သေးဘူး", chat_id=-1003795677289, message_id=9,
        reason="sensitive",
    )
    assert "t.me/c/3795677289/9" in dm_group


def test_private_ai_settings_plumbing():
    # Settings fields + registry + API surface all carry the 4 keys.
    assert settings.private_ai_enabled is True
    assert settings.private_ai_user_cooldown_seconds == 60
    assert settings.private_ai_hourly_budget == 40
    assert settings.private_ai_daily_budget == 300
    ds_src = _read("app/services/dynamic_settings.py")
    for key in ("private_ai_enabled", "private_ai_user_cooldown_seconds",
                "private_ai_hourly_budget", "private_ai_daily_budget"):
        assert f'"{key}"' in ds_src
    api_src = _read("app/webapp/api.py")
    assert "private_ai_enabled: Optional[bool] = None" in api_src
    assert '"private_ai_enabled": bool(getattr(settings, "private_ai_enabled", True))' in api_src
    from app.webapp.api import _validate_admin_settings_updates

    cleaned = _validate_admin_settings_updates(
        {"private_ai_user_cooldown_seconds": 30, "private_ai_hourly_budget": 10,
         "private_ai_daily_budget": 100}
    )
    assert cleaned == {
        "private_ai_user_cooldown_seconds": 30,
        "private_ai_hourly_budget": 10,
        "private_ai_daily_budget": 100,
    }


def test_private_ai_admin_ui_block():
    index = _read("app/webapp/static/index.html")
    for el_id in ("adm-set-private-ai", "adm-set-private-ai-cooldown",
                  "adm-set-private-ai-hourly", "adm-set-private-ai-daily"):
        assert f'id="{el_id}"' in index
    i18n_js = _read("app/webapp/static/js/i18n.js")
    for key in ("settings_private_ai", "settings_private_ai_hint",
                "settings_private_ai_cooldown", "settings_private_ai_hourly_budget",
                "settings_private_ai_daily_budget"):
        assert i18n_js.count(key) >= 2, key  # my + en
    js = _read("app/webapp/static/js/app.js")
    assert "private_ai_enabled: privateAi," in js
    assert 'document.getElementById("adm-set-private-ai")' in js


def test_private_deferral_locale_keys_exist_both_languages():
    for lang in ("my", "en"):
        assert "admin" in t("private_ai_deferral_sensitive", lang).lower()
        assert "admin" in t("private_ai_deferral_unsure", lang).lower()


def test_private_router_registered_on_the_shared_last_router():
    src = _read("app/handlers/group_admin.py")
    assert '@group_admin_router.message(F.chat.type == ChatType.PRIVATE, F.text)' in src
    assert "async def handle_private_message" in src
    # The group handler is untouched in shape (multi-line decorator).
    assert "F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})" in src
    assert "async def handle_group_message" in src
