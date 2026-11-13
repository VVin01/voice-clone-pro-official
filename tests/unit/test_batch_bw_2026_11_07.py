"""Batch BW (2026-11-07): unified per-generation character limits —
FREE 1,000 / PAID 5,000 across Voice Clone AND TTS (owner decision
2026-11-07 00:01).

Before BW the standalone TTS tab was the one surface that ignored the
tier pair: tts_service enforced a flat TTS_MAX_CHARS = 5000 for
everyone, _tts_error_detail named that flat 5,000, and the tab's
counter was hardcoded "x / 5,000" — so a FREE user could submit
5,000 chars in TTS while the voice paths rejected them at 1,000.

BW:
* create_tts_job enforces tts_character_limit(user): the caller's
  tier limit (settings.character_limit_free / _purchased), bounded by
  settings.infrastructure_safety_character_limit; admins are bound
  only by the safety ceiling (mirroring queue.py's admin branch).
  The reason code stays TEXT_EXCEEDS_CHARACTER_LIMIT.
* _tts_error_detail names the CALLER's own limit; FREE users also get
  the upsell naming the purchased limit (voice-message parity).
* The TTS counter paints the signed-in user's own limit (same
  expression + i18n unit as the voice counter, repainted on profile
  load) and generateTTS carries the same client-side tier guard the
  voice submit has.
* The voice-side enforcement is untouched (pinned below).
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models.enums import JobStatus, JobType
from app.services import tts_service
from app.services.ledger import credit_purchased_minutes, get_or_create_user
from app.services.tts_service import create_tts_job, tts_character_limit
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
QUEUE_PY = (ROOT / "app/services/queue.py").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _bw_limits(monkeypatch):
    # The exact BW matrix values (the shared conftest already forces
    # 1000/5000; pin them here too so this file's boundaries stay
    # exact if the shared defaults ever move).
    monkeypatch.setattr(settings, "character_limit_free", 1000)
    monkeypatch.setattr(settings, "character_limit_purchased", 5000)
    monkeypatch.setattr(settings, "infrastructure_safety_character_limit", 5000)
    # Isolate the character cap from the daily-minutes cap (0 = the
    # tab's unlimited setting) so ONLY the char limit can reject here.
    monkeypatch.setattr(settings, "tts_daily_limit_minutes_free", 0)
    monkeypatch.setattr(settings, "tts_daily_limit_minutes_paid", 0)
    yield


def _submit(db_session, user_id, text, **kw):
    kw.setdefault("engine", "gemini")
    kw.setdefault("voice", "Charon")
    kw.setdefault("speed_rate", "+12%")
    return create_tts_job(db_session, user_id, text=text, **kw)


def _between(src: str, start: str, end: str) -> str:
    i = src.index(start)
    j = src.index(end, i)
    return src[i:j]


# ── BW-1: per-tier enforcement in create_tts_job ──────────────────────


@pytest.mark.asyncio
async def test_bw_tts_free_at_limit_ok(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=559101)
    job, reason = await _submit(db_session, user.id, text="x" * 1000)
    assert reason == "QUEUED"
    assert job is not None
    assert job.job_type == JobType.TTS.value
    assert job.status == JobStatus.QUEUED.value


@pytest.mark.asyncio
async def test_bw_tts_free_over_limit_rejected(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=559102)
    job, reason = await _submit(db_session, user.id, text="x" * 1001)
    assert (job, reason) == (None, "TEXT_EXCEEDS_CHARACTER_LIMIT")


@pytest.mark.asyncio
async def test_bw_tts_purchased_at_limit_ok(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=559103)
    await credit_purchased_minutes(db_session, user_id=user.id, minutes=60)
    job, reason = await _submit(db_session, user.id, text="x" * 5000)
    assert reason == "QUEUED"
    assert job is not None and job.job_type == JobType.TTS.value


@pytest.mark.asyncio
async def test_bw_tts_purchased_over_limit_rejected(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=559104)
    await credit_purchased_minutes(db_session, user_id=user.id, minutes=60)
    job, reason = await _submit(db_session, user.id, text="x" * 5001)
    assert (job, reason) == (None, "TEXT_EXCEEDS_CHARACTER_LIMIT")


@pytest.mark.asyncio
async def test_bw_tts_admin_bound_only_by_safety_ceiling(db_session, monkeypatch):
    user, _ = await get_or_create_user(db_session, telegram_id=559105)
    monkeypatch.setattr(settings, "admin_ids_raw", str(user.id))
    # 5,000 chars is 5x the FREE tier limit — an admin is not bound
    # by it, only by the infrastructure safety ceiling (5,000).
    job, reason = await _submit(db_session, user.id, text="x" * 5000)
    assert reason == "QUEUED"
    assert job is not None


@pytest.mark.asyncio
async def test_bw_tts_admin_over_safety_ceiling_rejected(db_session, monkeypatch):
    user, _ = await get_or_create_user(db_session, telegram_id=559106)
    monkeypatch.setattr(settings, "admin_ids_raw", str(user.id))
    job, reason = await _submit(db_session, user.id, text="x" * 5001)
    assert (job, reason) == (None, "TEXT_EXCEEDS_CHARACTER_LIMIT")


# ── BW-2: the limit helper itself ─────────────────────────────────────


def test_bw_tts_character_limit_matrix(monkeypatch):
    free = SimpleNamespace(account_category="FREE")
    paid = SimpleNamespace(account_category="PURCHASED")
    assert tts_character_limit(free, is_admin=False) == 1000
    assert tts_character_limit(paid, is_admin=False) == 5000
    # Admin branch ignores the tier pair entirely.
    assert tts_character_limit(free, is_admin=True) == 5000
    # The safety ceiling caps an over-generous tier value.
    monkeypatch.setattr(settings, "character_limit_purchased", 8000)
    assert tts_character_limit(paid, is_admin=False) == 5000
    # A smaller tier value passes through untouched.
    monkeypatch.setattr(settings, "character_limit_free", 300)
    assert tts_character_limit(free, is_admin=False) == 300


def test_bw_tts_service_flat_cap_removed():
    create_src = inspect.getsource(tts_service.create_tts_job)
    assert "tts_character_limit(user, is_admin=is_admin)" in create_src
    assert "TTS_MAX_CHARS" not in create_src
    assert not hasattr(tts_service, "TTS_MAX_CHARS")


# ── BW-3: the rejection message names the caller's own limit ─────────


def test_bw_tts_error_detail_free_names_free_limit_and_upsell():
    user = SimpleNamespace(id=559201, account_category="FREE")
    detail = api_mod._tts_error_detail("TEXT_EXCEEDS_CHARACTER_LIMIT", user)
    assert "1,000" in detail
    # The upsell names the purchased limit.
    assert "5,000" in detail
    assert "VIP" in detail


def test_bw_tts_error_detail_purchased_names_purchased_limit_no_upsell():
    user = SimpleNamespace(id=559202, account_category="PURCHASED")
    detail = api_mod._tts_error_detail("TEXT_EXCEEDS_CHARACTER_LIMIT", user)
    assert "5,000" in detail
    assert "အဆင့်မြှင့်တင်ပါ" not in detail


def test_bw_tts_error_detail_admin_names_safety_ceiling_no_upsell(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", "559203")
    user = SimpleNamespace(id=559203, account_category="FREE")
    detail = api_mod._tts_error_detail("TEXT_EXCEEDS_CHARACTER_LIMIT", user)
    assert "5,000" in detail
    assert "အဆင့်မြှင့်တင်ပါ" not in detail


# ── BW-4: frontend — per-user counter, submit guard, cache token ────


def test_bw_tts_counter_uses_own_limit():
    body = _between(APP_JS, "function updateTtsCharCount", "function insertTtsSampleText")
    assert "/ 5,000" not in body
    assert "state.user?.character_limit" in body
    assert "character_limit_purchased ?? 5000" in body
    assert "character_limit_free ?? 1000" in body
    assert 't("char_counter_unit")' in body


def test_bw_tts_submit_guard_mirrors_voice_guard():
    body = _between(APP_JS, "async function generateTTS", "window.generateTTS")
    assert "maxChars" in body
    assert "character_limit_free ?? 1000" in body
    assert 'tf("text_too_long"' in body


def test_bw_tts_counter_repaints_on_profile_load():
    # init + sample-insert + clear + the BW profile-load repaint
    # (+2: Batch BZ's first-visit prefill repaint, and BZ's input
    # listener became a wrapper that calls it after settling the
    # prefill flag — previously a bare function reference).
    assert APP_JS.count("updateTtsCharCount();") == 6


def test_bw_index_counter_default_and_cache_token():
    assert 'id="tts-char-count">0 / 1000 chars<' in INDEX_HTML
    assert INDEX_HTML.count("20261113_r145") == 4


# ── BW-5: the voice side is unchanged ─────────────────────────────────


def test_bw_voice_side_enforcement_untouched():
    # queue.py's choke point still enforces the same tier pair with
    # the same reason code (BW only brought TTS up to parity).
    assert "settings.character_limit_purchased" in QUEUE_PY
    assert "settings.character_limit_free" in QUEUE_PY
    assert 'return None, "TEXT_EXCEEDS_CHARACTER_LIMIT"' in QUEUE_PY
    assert 'return None, "TEXT_EXCEEDS_INFRASTRUCTURE_LIMIT"' in QUEUE_PY
