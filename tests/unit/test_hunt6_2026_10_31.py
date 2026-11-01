"""
test_hunt6_2026_10_31.py - Deep hunt #6 fixes.

Covered:
(W2) Webhook intake refuses oversized bodies (Content-Length fast path
     and streamed/chunked bodies) with 413 before any parsing, and the
     dispatcher is never invoked for them.
(W3) A BOT_WEBHOOK_PATH colliding with Mini App routes is a webhook
     config problem -> resolve_bot_mode falls back to polling.
(W1) The FastAPI lifespan shutdown drains in-flight webhook update
     tasks instead of silently dropping acked updates.
(E1) Source/download errors are classified into curated Burmese
     user messages (TikTok no-formats, share-site 403, unsupported,
     removed/private, transient network) across get_info /
     download_video / download_audio_for_transcription, and the
     Downloader fetch endpoint passes a curated detail through while
     still masking raw extractor text.
(B1) The bot's global error handler no longer leaves users with
     silence on unhandled exceptions: it sends the generic notice
     (callback answer or chat message) and reports handled.
(Q1) The voice worker's truncation gate: implausibly short audio for
     a long text is rejected; legitimate paces (incl. speed presets)
     pass.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.webapp.server import webapp_app
from app.webapp import telegram_webhook as tw
from app.webapp.telegram_webhook import (
    MAX_UPDATE_BODY_BYTES,
    SECRET_HEADER,
    attach_bot,
    detach_bot,
    recent_update_ids,
    resolve_bot_mode,
    webhook_config_problems,
)


# ── Shared webhook fixture (same shape as test_telegram_webhook) ──────


@pytest.fixture(autouse=True)
def clean_webhook_state():
    detach_bot()
    recent_update_ids.clear()
    orig_mode = settings.bot_mode
    orig_secret = getattr(settings, "telegram_webhook_secret")
    orig_path = settings.bot_webhook_path
    orig_base = settings.public_base_url
    yield
    detach_bot()
    recent_update_ids.clear()
    settings.bot_mode = orig_mode
    setattr(settings, "telegram_webhook_secret", orig_secret)
    settings.bot_webhook_path = orig_path
    settings.public_base_url = orig_base


def _attach_mock_dispatcher():
    settings.bot_mode = "webhook"
    setattr(settings, "telegram_webhook_secret", "hunt6" + "_secret")
    settings.bot_webhook_path = "/telegram/webhook"
    mock_bot = MagicMock()
    mock_bot.id = 123456
    mock_dispatcher = MagicMock()
    mock_dispatcher.feed_webhook_update = AsyncMock(return_value=None)
    mock_dispatcher.feed_update = AsyncMock(return_value=None)
    attach_bot(mock_bot, mock_dispatcher)
    return mock_dispatcher


# ── W2: oversized webhook bodies ──────────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_oversized_content_length_rejected_413():
    mock_dispatcher = _attach_mock_dispatcher()
    big_body = b"x" * (MAX_UPDATE_BODY_BYTES + 1)
    transport = ASGITransport(app=webapp_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/telegram/webhook",
            content=big_body,
            headers={SECRET_HEADER: "hunt6_secret", "Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert not mock_dispatcher.feed_webhook_update.called
    assert not mock_dispatcher.feed_update.called


@pytest.mark.asyncio
async def test_webhook_oversized_streamed_body_rejected_413():
    """No honest Content-Length: the streaming cap must still bite."""
    mock_dispatcher = _attach_mock_dispatcher()

    async def _chunks():
        chunk = b"y" * 65536
        for _ in range((MAX_UPDATE_BODY_BYTES // 65536) + 2):
            yield chunk

    transport = ASGITransport(app=webapp_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/telegram/webhook",
            content=_chunks(),
            headers={SECRET_HEADER: "hunt6_secret", "Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert not mock_dispatcher.feed_webhook_update.called
    assert not mock_dispatcher.feed_update.called


# ── W3: webhook path collision guard ──────────────────────────────────


@pytest.mark.parametrize(
    "bad_path", ["/", "/api", "/api/user/me", "/api/recap/generate", "/static/app.js"]
)
def test_webhook_path_collision_is_config_problem(bad_path):
    settings.bot_mode = "webhook"
    setattr(settings, "telegram_webhook_secret", "hunt6" + "_secret")
    settings.public_base_url = "https://example.onrender.com"
    settings.bot_webhook_path = bad_path
    problems = webhook_config_problems()
    assert any("collides" in p for p in problems), problems
    assert resolve_bot_mode(web_server_in_process=True) == "polling"


def test_webhook_dedicated_path_has_no_collision_problem():
    settings.bot_mode = "webhook"
    setattr(settings, "telegram_webhook_secret", "hunt6" + "_secret")
    settings.public_base_url = "https://example.onrender.com"
    settings.bot_webhook_path = "/telegram/webhook"
    assert webhook_config_problems() == []
    assert resolve_bot_mode(web_server_in_process=True) == "webhook"


# ── W1: lifespan shutdown drains webhook background tasks ─────────────


@pytest.mark.asyncio
async def test_lifespan_shutdown_drains_webhook_tasks():
    completed = []

    async def _slow_update():
        await asyncio.sleep(0.3)
        completed.append(True)

    async with webapp_app.router.lifespan_context(webapp_app):
        task = asyncio.create_task(_slow_update())
        tw._background_tasks.add(task)
        task.add_done_callback(tw._background_tasks.discard)
    # Shutdown returned only after the in-flight update finished.
    assert completed == [True]
    assert task.done()


# ── E1: source error classification ───────────────────────────────────

from app.services.downloader import MediaDownloader, classify_source_error

TIKTOK_URL = "https://www.tiktok.com/@someone/video/7234567890123456789"
VIDMATE_URL = "https://vidmate.example.com/share/abc123"


def test_classify_tiktok_no_formats():
    msg = classify_source_error(
        "ERROR: [tiktok] 7234567890: No video formats found!", TIKTOK_URL
    )
    assert msg is not None and msg.startswith("⚠️")
    assert "TikTok" in msg


def test_classify_share_site_403_blocked():
    msg = classify_source_error("HTTP Error 403: Forbidden", VIDMATE_URL)
    assert msg is not None and msg.startswith("⚠️")
    assert "ပိတ်ထားပါသည်" in msg


def test_classify_unsupported_url():
    msg = classify_source_error("ERROR: Unsupported URL: https://x.example/v", "https://x.example/v")
    assert msg is not None and "မထောက်ပံ့သေးပါ" in msg


def test_classify_removed_or_private():
    msg = classify_source_error("ERROR: Video unavailable", "https://www.youtube.com/watch?v=abc")
    assert msg is not None and "private" in msg


def test_classify_network_transient():
    msg = classify_source_error(
        "ERROR: Unable to download webpage: HTTP Error 503: Service Unavailable",
        "https://www.youtube.com/watch?v=abc",
    )
    assert msg is not None and "ကွန်ရက်" in msg


def test_classify_unknown_returns_none():
    assert classify_source_error("ERROR: some brand-new failure mode", TIKTOK_URL) is None


class _FailingYDL:
    """yt_dlp.YoutubeDL stand-in whose extract always fails."""

    exc = Exception("ERROR: [tiktok] 7234: No video formats found!")

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        raise self.exc

    def prepare_filename(self, info):
        return "unused.mp4"


def _patch_ydl(monkeypatch, exc=None):
    import app.services.downloader as dl_mod

    monkeypatch.setattr(dl_mod, "unshorten_url", lambda u: u)
    if exc is not None:
        _FailingYDL.exc = exc
    monkeypatch.setattr(dl_mod.yt_dlp, "YoutubeDL", _FailingYDL)
    return dl_mod


def test_get_info_raises_classified_message(monkeypatch, tmp_path):
    _patch_ydl(monkeypatch)
    dl = MediaDownloader(download_dir=str(tmp_path))
    with pytest.raises(RuntimeError) as ei:
        dl.get_info(TIKTOK_URL)
    assert str(ei.value).startswith("⚠️")
    assert "No video formats" not in str(ei.value)


def test_download_video_raises_classified_message(monkeypatch, tmp_path):
    _patch_ydl(monkeypatch)
    dl = MediaDownloader(download_dir=str(tmp_path))
    with pytest.raises(RuntimeError) as ei:
        dl.download_video(TIKTOK_URL, target_dir=str(tmp_path))
    assert str(ei.value).startswith("⚠️")
    assert "No video formats" not in str(ei.value)


def test_download_audio_for_transcription_raises_classified(monkeypatch, tmp_path):
    _patch_ydl(monkeypatch, Exception("HTTP Error 403: Forbidden"))
    dl = MediaDownloader(download_dir=str(tmp_path))
    with pytest.raises(RuntimeError) as ei:
        dl.download_audio_for_transcription(VIDMATE_URL, target_dir=str(tmp_path))
    assert str(ei.value).startswith("⚠️")
    assert "ပိတ်ထားပါသည်" in str(ei.value)


# ── E1b: Downloader fetch endpoint detail pass-through ────────────────


@pytest.fixture()
def dl_env(tmp_path, monkeypatch):
    """Same sandbox shape as test_downloader_format_delivery.dl_env."""
    import app.services.downloader as dl_mod
    import app.services.url_safety as safety_mod
    import app.webapp.api as api

    root = tmp_path / "downloads"
    monkeypatch.setattr(api, "_dl_root", lambda: root)
    monkeypatch.setattr(dl_mod, "unshorten_url", lambda u: u)
    monkeypatch.setattr(safety_mod, "validate_public_url", lambda u: u)
    return api


def _fetch(api, fmt="video", uid=111, url="https://www.tiktok.com/@a/video/1"):
    req = api.DlFetchRequest(url=url, format=fmt)
    return asyncio.run(
        api.fetch_downloader_format(req, user=SimpleNamespace(id=uid), _gate=SimpleNamespace(id=uid))
    )


def test_fetch_passes_through_curated_detail(dl_env, monkeypatch):
    import app.services.downloader as dl_mod

    curated = "⚠️ TikTok က ကျွန်တော်တို့ ဆာဗာမှ ဗီဒီယို ဆွဲယူခြင်းကို ပိတ်ထားပါသည်။"

    class _CuratedFail:
        def __init__(self, *a, **kw):
            pass

        def download_video(self, url, target_dir=None):
            raise RuntimeError(curated)

    monkeypatch.setattr(dl_mod, "MediaDownloader", _CuratedFail)
    with pytest.raises(HTTPException) as ei:
        _fetch(dl_env)
    assert ei.value.status_code == 400
    assert ei.value.detail == curated


def test_fetch_still_masks_raw_extractor_text(dl_env, monkeypatch):
    import app.services.downloader as dl_mod

    class _RawFail:
        def __init__(self, *a, **kw):
            pass

        def download_video(self, url, target_dir=None):
            raise RuntimeError("ERROR: [generic] raw yt-dlp internals here")

    monkeypatch.setattr(dl_mod, "MediaDownloader", _RawFail)
    with pytest.raises(HTTPException) as ei:
        _fetch(dl_env)
    assert ei.value.status_code == 400
    assert "raw yt-dlp" not in ei.value.detail
    assert ei.value.detail.startswith("ဒေါင်းလုဒ် မအောင်မြင်ပါ")


# ── B1: bot global error handler notifies the user ────────────────────


class _NullSessionFactory:
    """Session factory whose session finds no user (language default)."""

    def __call__(self):
        return self

    async def __aenter__(self):
        session = MagicMock()
        session.get = AsyncMock(return_value=None)
        return session

    async def __aexit__(self, *a):
        return False


def _error_handler(dp):
    return dp.error.handlers[0].call


@pytest.mark.asyncio
async def test_global_error_handler_notifies_on_message_update():
    from aiogram import Dispatcher
    from aiogram.types import ErrorEvent, Message, Update
    from aiogram.types import User as TgUser

    from app.handlers import setup_handlers

    dp = Dispatcher()
    setup_handlers(dp, session_factory=_NullSessionFactory())

    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=424242, is_bot=False, first_name="Tester")
    msg.answer = AsyncMock()
    update = MagicMock(spec=Update)
    update.message = msg
    update.callback_query = None
    update.my_chat_member = None
    event = ErrorEvent(update=update, exception=RuntimeError("boom in handler"))

    result = await _error_handler(dp)(event)
    assert result is True
    msg.answer.assert_awaited_once()
    assert "ချို့ယွင်း" in msg.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_global_error_handler_answers_callback_query():
    from aiogram import Dispatcher
    from aiogram.types import CallbackQuery, ErrorEvent, Update
    from aiogram.types import User as TgUser

    from app.handlers import setup_handlers

    dp = Dispatcher()
    setup_handlers(dp, session_factory=_NullSessionFactory())

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=434343, is_bot=False, first_name="Tester")
    cb.answer = AsyncMock()
    update = MagicMock(spec=Update)
    update.message = None
    update.callback_query = cb
    update.my_chat_member = None
    event = ErrorEvent(update=update, exception=ValueError("bad state"))

    result = await _error_handler(dp)(event)
    assert result is True
    cb.answer.assert_awaited_once()
    assert cb.answer.call_args.kwargs.get("show_alert") is True


# ── Q1: voice truncation gate ─────────────────────────────────────────

from app.worker import _implausible_audio_reason

BURMESE = "က"  # one code point per char; only length matters here


def test_gate_catches_2026_10_29_incident_shape():
    # 847 chars delivered in 23 s = 36.8 chars/s at normal speed.
    assert _implausible_audio_reason(BURMESE * 847, 23, "normal") is not None


def test_gate_allows_legit_fallback_pace():
    # Edge fallback racing pace ≈ 22 chars/s: 847 chars ≈ 39 s.
    assert _implausible_audio_reason(BURMESE * 847, 39, "normal") is None
    assert _implausible_audio_reason(BURMESE * 682, 34, "normal") is None


def test_gate_allows_fast_preset_headroom():
    assert _implausible_audio_reason(BURMESE * 847, 30, "fast") is None
    assert _implausible_audio_reason(BURMESE * 847, 18, "fast") is not None


def test_gate_slow_preset_and_short_text_exemptions():
    assert _implausible_audio_reason(BURMESE * 688, 34, "slow") is None
    assert _implausible_audio_reason(BURMESE * 120, 2, "normal") is None
    assert _implausible_audio_reason(BURMESE * 500, 0, "normal") is None
