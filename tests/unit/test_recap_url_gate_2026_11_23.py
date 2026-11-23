"""Part C — Recap Step 1 strict URL gate (2026-11-23) unit test suite.

Pins:
  1. POST /api/downloader/extract returns HTTP 422 (no shallow fabricated
     success payload) when extraction raises, returns a generic title, or
     returns a missing/non-positive duration; returns 200 verified payload
     for a real video.
  2. Server-side backstop in POST /api/recap/generate rejects unverifiable
     HTTP(S) URLs with HTTP 422 and the exact same user-facing error,
     creating ZERO Job rows and charging ZERO credits/minutes.
  3. Direct-upload local paths remain unaffected by the HTTP URL gate.
  4. Exact MY + EN error copy present; revoked unverified-proceed copy,
     markup, CSS, and functions are completely removed.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.enums import AccountCategory
from app.models.schema import Job, RecapCreditLedgerEvent
from app.services.downloader import MediaDownloader
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")


# ── 1. Info endpoint strict verification ──────────────────────────────────


@pytest.mark.asyncio
async def test_extract_endpoint_returns_422_on_extraction_error(db_session, monkeypatch):
    user, _ = await get_or_create_user(db_session, telegram_id=930001)
    user.language_code = "my"
    monkeypatch.setattr("app.services.downloader.unshorten_url", lambda u: u)
    monkeypatch.setattr("app.services.url_safety.validate_public_url", lambda u: u)

    def _boom(self, url):
        raise RuntimeError("Video unavailable")

    monkeypatch.setattr(MediaDownloader, "get_info", _boom)

    with pytest.raises(HTTPException) as exc_info:
        await api_mod.extract_media_info(
            api_mod.ExtractMediaRequest(url="https://www.youtube.com/watch?v=xxxxxxxxxxx"),
            user=user,
            _gate=user,
        )

    assert exc_info.value.status_code == 422
    assert "Link စစ်ဆေး၍ မရပါ" in exc_info.value.detail
    assert "ဒီ link ကနေ ဗီဒီယို ရှာမတွေ့ပါ" in exc_info.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_info",
    [
        {"title": "YouTube Content", "duration": 120, "platform": "YouTube"},
        {"title": "Web Media Content", "duration": 90, "platform": "Web Media"},
        {"title": "Video", "duration": 60, "platform": "Video"},
        {"title": "media", "duration": 45, "platform": "YouTube"},
        {"title": "", "duration": 45, "platform": "YouTube"},
        {"title": "Actual Real Title", "duration": None, "platform": "YouTube"},
        {"title": "Actual Real Title", "duration": 0, "platform": "YouTube"},
        {"title": "Actual Real Title", "duration": -10, "platform": "YouTube"},
        {
            "title": "Example Domain",
            "duration": 30,
            "platform": "Web Media",
            "raw_info": {"formats": []},
        },
    ],
)
async def test_extract_endpoint_rejects_shallow_or_non_video_metadata(
    db_session, monkeypatch, bad_info
):
    user, _ = await get_or_create_user(db_session, telegram_id=930002)
    user.language_code = "en"
    monkeypatch.setattr("app.services.downloader.unshorten_url", lambda u: u)
    monkeypatch.setattr("app.services.url_safety.validate_public_url", lambda u: u)
    monkeypatch.setattr(MediaDownloader, "get_info", lambda self, u: dict(bad_info))

    with pytest.raises(HTTPException) as exc_info:
        await api_mod.extract_media_info(
            api_mod.ExtractMediaRequest(url="https://example.com/notavideo"),
            user=user,
            _gate=user,
        )

    assert exc_info.value.status_code == 422
    assert "Couldn't verify this link" in exc_info.value.detail
    assert "No video was found at this link" in exc_info.value.detail


@pytest.mark.asyncio
async def test_extract_endpoint_succeeds_for_real_verified_video(db_session, monkeypatch):
    user, _ = await get_or_create_user(db_session, telegram_id=930003)
    user.language_code = "my"
    monkeypatch.setattr("app.services.downloader.unshorten_url", lambda u: u)
    monkeypatch.setattr("app.services.url_safety.validate_public_url", lambda u: u)
    monkeypatch.setattr(
        MediaDownloader,
        "get_info",
        lambda self, u: {
            "title": "Real Verified Short Film",
            "duration": 84,
            "platform": "YouTube",
            "uploader": "Studio Channel",
            "thumbnail": "https://i.ytimg.com/vi/PndIBLHg33E/hqdefault.jpg",
            "width": 1280,
            "height": 720,
            "has_subtitles": True,
            "raw_info": {
                "formats": [{"vcodec": "avc1", "acodec": "mp4a"}],
            },
        },
    )

    resp = await api_mod.extract_media_info(
        api_mod.ExtractMediaRequest(url="https://www.youtube.com/watch?v=PndIBLHg33E"),
        user=user,
        _gate=user,
    )

    assert resp["success"] is True
    assert resp["metadata_verified"] is True
    assert resp["title"] == "Real Verified Short Film"
    assert resp["duration_seconds"] == 84.0
    assert resp["estimated_duration"] == "01:24"
    assert "MP4 (Full HD Video)" in resp["formats_available"]


# ── 2. Server-side backstop in create_recap_job ───────────────────────────


@pytest.mark.asyncio
async def test_create_recap_job_rejects_unverifiable_url_no_job_no_charge(
    db_session, monkeypatch
):
    user, _ = await get_or_create_user(db_session, telegram_id=930010)
    user.language_code = "my"
    user.account_category = AccountCategory.PURCHASED.value
    user.recap_credits_seconds = 600
    await db_session.commit()

    before_ledger = (
        await db_session.execute(
            select(RecapCreditLedgerEvent).where(RecapCreditLedgerEvent.user_id == user.id)
        )
    ).scalars().all()
    before_ledger_count = len(before_ledger)

    monkeypatch.setattr("app.services.downloader.unshorten_url", lambda u: u)
    monkeypatch.setattr("app.services.url_safety.validate_public_url", lambda u: u)

    def _fail_info(self, url):
        raise RuntimeError("ERROR: Video unavailable")

    monkeypatch.setattr(MediaDownloader, "get_info", _fail_info)

    req = api_mod.RecapConfigRequest(
        url="https://www.youtube.com/watch?v=xxxxxxxxxxx",
        duration_seconds=90,  # Client attempts to spoof a positive duration
        title="Spoofed Title",
        tts_engine="edge",
    )

    with pytest.raises(HTTPException) as exc_info:
        await api_mod.create_recap_job(req=req, user=user, db=db_session, _gate=user)

    assert exc_info.value.status_code == 422
    assert "Link စစ်ဆေး၍ မရပါ" in exc_info.value.detail
    assert "ဒီ link ကနေ ဗီဒီယို ရှာမတွေ့ပါ" in exc_info.value.detail

    # Zero Job rows created
    jobs = (
        await db_session.execute(select(Job).where(Job.user_id == user.id))
    ).scalars().all()
    assert jobs == []

    # Zero new credit ledger events and untouched balance
    ledger_rows = (
        await db_session.execute(
            select(RecapCreditLedgerEvent).where(RecapCreditLedgerEvent.user_id == user.id)
        )
    ).scalars().all()
    assert len(ledger_rows) == before_ledger_count
    await db_session.refresh(user)
    assert user.recap_credits_seconds == 600


@pytest.mark.asyncio
async def test_create_recap_job_rejects_shallow_metadata_no_job_no_charge(
    db_session, monkeypatch
):
    user, _ = await get_or_create_user(db_session, telegram_id=930011)
    user.language_code = "en"
    user.account_category = AccountCategory.PURCHASED.value
    user.recap_credits_seconds = 600
    await db_session.commit()

    monkeypatch.setattr("app.services.downloader.unshorten_url", lambda u: u)
    monkeypatch.setattr("app.services.url_safety.validate_public_url", lambda u: u)
    monkeypatch.setattr(
        MediaDownloader,
        "get_info",
        lambda self, u: {"title": "Web Media Content", "duration": 0, "platform": "Web Media"},
    )

    req = api_mod.RecapConfigRequest(
        url="https://example.com/notavideo",
        duration_seconds=60,
        tts_engine="edge",
    )

    with pytest.raises(HTTPException) as exc_info:
        await api_mod.create_recap_job(req=req, user=user, db=db_session, _gate=user)

    assert exc_info.value.status_code == 422
    assert "Couldn't verify this link" in exc_info.value.detail
    jobs = (
        await db_session.execute(select(Job).where(Job.user_id == user.id))
    ).scalars().all()
    assert jobs == []
    await db_session.refresh(user)
    assert user.recap_credits_seconds == 600


# ── 3. Structural assertions (revoked unverified-proceed design gone) ─────


def test_revoked_unverified_proceed_design_and_copy_removed():
    # Exact required MY + EN error copy present
    assert 'recap_url_verify_err_title: "Link စစ်ဆေး၍ မရပါ"' in I18N_JS
    assert (
        'recap_url_verify_err_body: "ဒီ link ကနေ ဗီဒီယို ရှာမတွေ့ပါ။ Link ကို စစ်ပြီး ပြန်စမ်းပါ — ဒါမှမဟုတ် ဗီဒီယိုဖိုင် တိုက်ရိုက် တင်ပါ။"'
        in I18N_JS
    )
    assert 'recap_url_verify_err_title: "Couldn\'t verify this link"' in I18N_JS
    assert (
        'recap_url_verify_err_body: "No video was found at this link. Check the link and try again — or upload the video file directly."'
        in I18N_JS
    )

    # Error card exists in Step 1 markup
    assert 'id="recap-url-error-card"' in INDEX_HTML
    assert 'id="recap-url-error-title"' in INDEX_HTML
    assert 'id="recap-url-error-body"' in INDEX_HTML

    # Revoked unverified-proceed artifacts must be completely absent
    for dead_token in (
        "recap_preview_unverified_hint",
        "recap_unverified_warning",
        "recap_unverified_note",
        "recap-unverified-warning",
        "paintRecapUnverifiedWarning",
    ):
        assert dead_token not in INDEX_HTML, f"Found dead token {dead_token} in index.html"
        assert dead_token not in APP_JS, f"Found dead token {dead_token} in app.js"
        assert dead_token not in I18N_JS, f"Found dead token {dead_token} in i18n.js"
        assert dead_token not in STYLE_CSS, f"Found dead token {dead_token} in style.css"

    # Server extract endpoint must NOT contain the old fabricated shallow fallback
    extract_src = inspect.getsource(api_mod.extract_media_info)
    assert '"metadata_verified": False' not in extract_src
    assert '"estimated_duration": "03:30"' not in extract_src
