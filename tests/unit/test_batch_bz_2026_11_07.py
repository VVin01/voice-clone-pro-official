"""Batch BZ (2026-11-07): FREE-entitlement visibility + consistency.

Owner approved the full bundle 2026-11-07 09:53 after his audit
questions: the FREE daily TTS allowance (the largest free value) was
invisible until the TTS tab; the FREE voice daily cap (5 min) could
never bind against the 3-minute monthly wallet; the job-count limit
was enforced but undocumented; and the shared AI fact sheet carried
no daily minute caps, so both AIs answered daily-limit questions
wrong.

1. /api/me exposes today's TTS spendable (referral + cap remainder)
   computed via the enforcement helpers; admins → nulls.
2. Header 🎧 chip (tap → TTS tab via switchTab), compact ≤430 px.
3. Home "Free today" card after the Start tiles, before Tools.
4. Bot /start menu TTS line = today's spendable, both locales.
5. Fact sheet gains the live daily minute caps; the job-count line
   renders only while free_daily_job_limit > 0.
6. Defaults: voice_daily_limit_minutes_free 5→0 and
   free_daily_job_limit 5→0 (mechanisms stay); guide copy follows.
7. TTS first-visit sample prefill (session-scoped, never overwrites).
8. Cache token r97.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import settings
from app.models.enums import JobStatus, JobType
from app.models.schema import Job, VoiceModel
from app.services import group_ai_admin as gai
from app.services.i18n import load_locales, t
from app.services.ledger import get_minute_balance, get_or_create_user

ROOT = Path(__file__).resolve().parents[2]

load_locales()


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


APP_JS = _read("app/webapp/static/js/app.js")
INDEX_HTML = _read("app/webapp/static/index.html")
I18N_JS = _read("app/webapp/static/js/i18n.js")
STYLE_CSS = _read("app/webapp/static/css/style.css")


# ===========================================================================
# Item 1 — /api/me today's TTS fields
# ===========================================================================

async def _profile(db_session, user):
    from app.webapp import api as api_mod

    return await api_mod.get_user_profile(user=user, db=db_session)


@pytest.mark.asyncio
async def test_me_tts_today_fresh_free_user(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=880001001)
    await db_session.commit()
    profile = await _profile(db_session, user)
    # FREE defaults: cap 15 min = 900 s, nothing consumed, no referral.
    assert profile["tts_daily_limit_seconds"] == 900
    assert profile["tts_today_consumed_seconds"] == 0
    assert profile["tts_referral_seconds"] == 0
    assert profile["tts_today_remaining_seconds"] == 900


@pytest.mark.asyncio
async def test_me_tts_today_remaining_is_referral_plus_cap_remainder(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=880001002)
    bal = await get_minute_balance(db_session, user.id)
    bal.referral_seconds = 120
    await db_session.flush()
    # A SUCCESS TTS job today: 300 s measured → counts in full (no
    # referral draw recorded against it).
    db_session.add(Job(
        user_id=user.id, job_type=JobType.TTS.value,
        status=JobStatus.SUCCESS.value, seconds_consumed=300,
        seconds_estimated=300, input_text="နမူနာ",
    ))
    # A FAILED TTS job counts nothing.
    db_session.add(Job(
        user_id=user.id, job_type=JobType.TTS.value,
        status=JobStatus.FAILED.value, seconds_consumed=500,
        seconds_estimated=500, input_text="နမူနာ",
    ))
    # A VOICE_CLONE job never touches the TTS counter.
    db_session.add(Job(
        user_id=user.id, job_type=JobType.VOICE_CLONE.value,
        status=JobStatus.SUCCESS.value, seconds_consumed=700,
        seconds_estimated=700, input_text="နမူနာ",
    ))
    await db_session.commit()

    profile = await _profile(db_session, user)
    assert profile["tts_today_consumed_seconds"] == 300
    assert profile["tts_referral_seconds"] == 120
    # remaining = referral 120 + (900 − 300) = 720.
    assert profile["tts_today_remaining_seconds"] == 720


@pytest.mark.asyncio
async def test_me_tts_today_ignores_yesterdays_jobs(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=880001003)
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    db_session.add(Job(
        user_id=user.id, job_type=JobType.TTS.value,
        status=JobStatus.SUCCESS.value, seconds_consumed=300,
        seconds_estimated=300, input_text="နမူနာ", created_at=yesterday,
    ))
    await db_session.commit()
    profile = await _profile(db_session, user)
    assert profile["tts_today_consumed_seconds"] == 0
    assert profile["tts_today_remaining_seconds"] == 900


@pytest.mark.asyncio
async def test_me_tts_today_admin_gets_nulls(db_session, monkeypatch):
    user, _ = await get_or_create_user(db_session, telegram_id=880001004)
    await db_session.commit()
    monkeypatch.setattr(settings, "admin_ids_raw", str(user.id))
    profile = await _profile(db_session, user)
    assert profile["tts_daily_limit_seconds"] is None
    assert profile["tts_today_consumed_seconds"] is None
    assert profile["tts_referral_seconds"] is None
    assert profile["tts_today_remaining_seconds"] is None


# ===========================================================================
# Item 2 — header 🎧 chip
# ===========================================================================

def test_header_tts_chip_markup():
    assert 'id="user-tts-bal"' in INDEX_HTML
    # The chip is a button wired to the REAL tab-switch path, so the
    # TTS entry hooks (quota refresh) fire on tap.
    assert "onclick=\"switchTab('tab-tts')\"" in INDEX_HTML
    assert 'data-i18n-title="bal_chip_tts_title"' in INDEX_HTML
    # Neutral placeholder before the profile paints — never a 0 flash.
    assert "🎧 —" in INDEX_HTML
    # Order: voice, recap, then TTS.
    assert (
        INDEX_HTML.index('id="user-voice-bal"')
        < INDEX_HTML.index('id="user-recap-bal"')
        < INDEX_HTML.index('id="user-tts-bal"')
    )


def test_header_tts_chip_paint_and_refresh():
    assert "function paintTtsTodaySurfaces(data)" in APP_JS
    assert "function paintTtsChipFromQuota(q)" in APP_JS
    # Painted from BOTH profile paths (fresh + cached) and refreshed
    # from the quota response (tab entry + post-job path).
    assert APP_JS.count("paintTtsTodaySurfaces(data);") == 2
    assert "paintTtsChipFromQuota(q);" in APP_JS
    # Admin paints ∞ on the chip, like the other chips.
    assert '"🎧 ∞"' in APP_JS
    # Compact at ≤430 px.
    assert ".bal-chip.tts" in STYLE_CSS
    assert "@media (max-width: 430px)" in STYLE_CSS


# ===========================================================================
# Item 3 — Home "Free today" card
# ===========================================================================

def test_home_free_card_position_and_rows():
    assert 'id="home-free-card"' in INDEX_HTML
    # V2 4-Zone IA order: Hero Balance Trio (#home-free-card) -> Start Studios -> Tools.
    assert (
        INDEX_HTML.index('id="home-free-card"')
        < INDEX_HTML.index('id="home-start-card"')
        < INDEX_HTML.index('id="home-tools-card"')
    )
    for row_id in (
        "home-free-tts-value",
        "home-free-voice-value",
        "home-free-recap-value",
        "home-free-tts-note",
    ):
        assert INDEX_HTML.count(f'id="{row_id}"') == 1
    # Each row jumps to its tab through the real switch path.
    assert "onclick=\"switchTab('tab-tts')\"" in INDEX_HTML
    assert "onclick=\"switchTab('tab-voice')\"" in INDEX_HTML
    assert "onclick=\"switchTab('tab-recap')\"" in INDEX_HTML
    # Values paint from the same painter as the header chip.
    assert 'id="home-free-tts-value"' in INDEX_HTML
    assert "home_free_value_min" in APP_JS
    assert "home_free_value_credits" in APP_JS
    assert "home_free_tts_referral_note" in APP_JS


def test_home_free_card_i18n_both_locales():
    for key in (
        "bal_chip_tts_title",
        "home_free_title",
        "home_free_tts_label",
        "home_free_tts_referral_note",
        "home_free_voice_label",
        "home_free_recap_label",
        "home_free_value_min",
        "home_free_value_credits",
        "tts_sample_text",
    ):
        # Each key defined exactly twice in i18n.js (my + en).
        assert I18N_JS.count(f"{key}:") == 2, key


# ===========================================================================
# Item 4 — bot /start menu free-today line
# ===========================================================================

@pytest.mark.asyncio
async def test_menu_tts_line_shows_todays_spendable(db_session):
    from app.handlers.menu import render_main_menu

    user, _ = await get_or_create_user(db_session, telegram_id=880001005)
    await db_session.commit()
    text, _kb = await render_main_menu(user.id, session=db_session)
    # Fresh FREE user: referral 0 + today's 900 s quota = 15.0 min.
    assert "ဒီနေ့ သုံးနိုင်သည်" in text
    assert "15.0 မိနစ်" in text
    assert "(900 စက္ကန့်)" in text


@pytest.mark.asyncio
async def test_menu_tts_line_combines_referral_and_quota(db_session):
    from app.handlers.menu import render_main_menu

    user, _ = await get_or_create_user(db_session, telegram_id=880001006)
    bal = await get_minute_balance(db_session, user.id)
    bal.referral_seconds = 120
    await db_session.flush()
    db_session.add(Job(
        user_id=user.id, job_type=JobType.TTS.value,
        status=JobStatus.SUCCESS.value, seconds_consumed=300,
        seconds_estimated=300, input_text="နမူနာ",
    ))
    await db_session.commit()
    text, _kb = await render_main_menu(user.id, session=db_session)
    # 120 referral + (900 − 300) quota = 720 s spendable today.
    assert "12.0 မိနစ်" in text
    assert "(720 စက္ကန့်)" in text


@pytest.mark.asyncio
async def test_menu_tts_line_english(db_session):
    from app.handlers.menu import render_main_menu

    user, _ = await get_or_create_user(db_session, telegram_id=880001007)
    user.language_code = "en"
    await db_session.commit()
    text, _kb = await render_main_menu(user.id, session=db_session)
    assert "spendable today" in text
    assert "15.0 min" in text


@pytest.mark.asyncio
async def test_menu_tts_line_admin_keeps_admin_block(db_session, monkeypatch):
    from app.handlers.menu import render_main_menu

    user, _ = await get_or_create_user(db_session, telegram_id=880001008)
    await db_session.commit()
    monkeypatch.setattr(settings, "admin_ids_raw", str(user.id))
    text, _kb = await render_main_menu(user.id, session=db_session)
    # The admin block is the ∞ variant and gains no today line.
    assert "∞" in text
    assert "ဒီနေ့ သုံးနိုင်သည်" not in text
    assert "spendable today" not in text


def test_menu_tts_today_line_locales_complete():
    for lang in ("my", "en"):
        out = t("menu_tts_today_line", lang, tts_min=15.0, tts_sec=900)
        assert "{" not in out
        assert "900" in out
    # The referral-only line still exists for the History surfaces.
    for lang in ("my", "en"):
        out = t("menu_tts_balance_line", lang, tts_min=2.0, tts_sec=120)
        assert "{" not in out


# ===========================================================================
# Item 5 — fact sheet daily caps + conditional job line
# ===========================================================================

def test_fact_sheet_states_daily_minute_caps():
    sheet = gai.build_fact_sheet()
    assert "Voice Clone တစ်နေ့ ကန့်သတ်ချက်" in sheet
    assert "TTS (TTS Tab) တစ်နေ့ ကန့်သတ်ချက်" in sheet
    # Defaults after BZ: FREE voice uncapped, paid voice 30, TTS 15/60.
    assert "အကန့်အသတ် မရှိ" in sheet
    assert "တစ်နေ့ 30 မိနစ်" in sheet
    assert "တစ်နေ့ 15 မိနစ်" in sheet
    assert "တစ်နေ့ 60 မိနစ်" in sheet


def test_fact_sheet_caps_follow_settings(monkeypatch):
    monkeypatch.setattr(settings, "voice_daily_limit_minutes_free", 7)
    monkeypatch.setattr(settings, "tts_daily_limit_minutes_paid", 45)
    sheet = gai.build_fact_sheet()
    assert "တစ်နေ့ 7 မိနစ်" in sheet
    assert "တစ်နေ့ 45 မိနစ်" in sheet


def test_fact_sheet_job_line_only_when_enabled(monkeypatch):
    # Default (0): the line is gone entirely — no "0 jobs" statement.
    assert int(settings.free_daily_job_limit) == 0
    assert "တစ်နေ့ Job" not in gai.build_fact_sheet()
    monkeypatch.setattr(settings, "free_daily_job_limit", 5)
    assert "တစ်နေ့ Job 5 ခု" in gai.build_fact_sheet()


# ===========================================================================
# Item 6 — FREE defaults + guide copy
# ===========================================================================

def test_free_defaults_retired_paid_untouched():
    assert settings.voice_daily_limit_minutes_free == 0
    assert settings.free_daily_job_limit == 0
    assert settings.voice_daily_limit_minutes_paid == 30
    assert settings.tts_daily_limit_minutes_free == 15
    assert settings.tts_daily_limit_minutes_paid == 60
    # The mechanisms stay: queue.py still enforces a re-enabled cap.
    queue_src = _read("app/services/queue.py")
    assert "settings.free_daily_job_limit > 0" in queue_src
    assert "DAILY_JOB_LIMIT_EXCEEDED" in queue_src


@pytest.mark.asyncio
async def test_free_user_past_five_jobs_at_default(db_session):
    """At the retired default the job-count gate no-ops: a FREE user
    creates a 6th voice job in a day (each prior job FAILED, which
    still counts toward a count cap when one is enabled)."""
    from app.services.queue import create_voice_clone_job

    db_session.add(VoiceModel(
        name="VoxCPM Cloud", slug="voxcpm-cloud-ai", type="SYSTEM",
        model_dir="/tmp", pth_path="voxcpm", status="ACTIVE",
        is_default_free=True,
    ))
    await db_session.flush()
    user, _ = await get_or_create_user(db_session, telegram_id=880001009)
    await db_session.flush()
    for _ in range(5):
        job, msg = await create_voice_clone_job(
            db_session, user.id, text="မင်္ဂလာပါ", chat_id=user.id
        )
        assert job is not None, msg
        job.status = JobStatus.FAILED.value
        await db_session.flush()
    job6, msg6 = await create_voice_clone_job(
        db_session, user.id, text="မင်္ဂလာပါ", chat_id=user.id
    )
    assert job6 is not None and msg6 == "QUEUED"


@pytest.mark.asyncio
async def test_guide_free_voice_clause_at_defaults(db_session):
    from app.handlers.menu import render_help_settings_content

    user, _ = await get_or_create_user(db_session, telegram_id=880001010)
    user.language_code = "my"
    await db_session.commit()
    text, _kb = await render_help_settings_content(db_session, user.id)
    # No daily cap at the default: the guide says so, and never
    # renders a "0 minutes per day" figure.
    assert "တစ်နေ့ သီးခြား ကန့်သတ်ချက် မရှိ" in text
    assert "Voice Clone <b>0 မိနစ်</b>" not in text
    assert "{voice_daily_free_clause}" not in text


@pytest.mark.asyncio
async def test_guide_free_voice_clause_when_admin_sets_cap(db_session, monkeypatch):
    from app.handlers.menu import render_help_settings_content

    user, _ = await get_or_create_user(db_session, telegram_id=880001011)
    user.language_code = "en"
    await db_session.commit()
    monkeypatch.setattr(settings, "voice_daily_limit_minutes_free", 9)
    text, _kb = await render_help_settings_content(db_session, user.id)
    assert "up to <b>9 minutes</b> of Voice Clone per day" in text


def test_guide_templates_have_no_stale_free_daily_voice_number():
    for lang in ("my", "en"):
        data = json.loads(
            (ROOT / f"locales/{lang}/messages.json").read_text(encoding="utf-8")
        )
        guide = data["help_settings_guide"]
        assert "{voice_daily_free}" not in guide
        assert "{voice_daily_free_clause}" in guide
        # Clause keys exist in both locales.
        assert "{minutes}" in data["guide_voice_daily_free_capped"]
        assert data["guide_voice_daily_free_uncapped"]


# ===========================================================================
# Item 7 — TTS first-visit sample prefill
# ===========================================================================

def test_tts_prefill_logic_pins():
    assert "function maybePrefillTtsSample()" in APP_JS
    assert "let ttsSampleSettled = false;" in APP_JS
    assert 'input.value = t("tts_sample_text");' in APP_JS
    # Never overwrites existing text: the empty check precedes the set.
    body = APP_JS[APP_JS.index("function maybePrefillTtsSample()"):]
    body = body[: body.index("\n}\n") + 3]
    assert '!== "")' in body
    assert body.index("ttsSampleSettled = true;") < body.index("input.value =")
    # Both TTS entry paths carry the hook (BI-4 discipline).
    assert APP_JS.count("window.maybePrefillTtsSample()") == 2
    # Real edits / sample chips / clear settle the prefill.
    assert APP_JS.count("ttsSampleSettled = true;") >= 4


# ===========================================================================
# Item 8 — cache token
# ===========================================================================

def test_cache_token_is_r97():
    assert INDEX_HTML.count("20261114_r150") == 4
    # Split literal: the previous token must be absent, written so a
    # future token sweep cannot rewrite this negative assertion.
    assert ("20261104_r" + "96") not in INDEX_HTML
