"""Downloader format cards: real delivery (2026-10-30).

Before this fix the Downloader tab's Video / MP3 / SRT cards were
toast-only placeholders (inline onclick showStudioToast). Now a tap
runs POST /api/downloader/fetch, which drives the existing
MediaDownloader into the persistent downloads store, and the file is
delivered through an ownership-checked download endpoint plus an
on-demand Telegram send — the same patterns as job outputs.

Pinned here:
* fetch stores the file + owner meta and returns a download URL,
* SRT with no obtainable subtitles -> honest 422 (no AI-generated
  substitute is ever produced by this path),
* the download endpoint 404s for non-owners and bad ids, serves the
  owner (and an admin),
* send-telegram 404s for non-owners, 413s over the Telegram limit,
  and sends via the bot for the owner,
* the extract fallback no longer claims has_subtitles=True,
* Mini App contracts: cards are wired to dlFetchFormat (no toast
  onclick left), the SRT card carries a per-item unavailable state,
  and the new i18n keys exist.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "app" / "webapp" / "static"


def _user(uid):
    return SimpleNamespace(id=uid)


@pytest.fixture()
def dl_env(tmp_path, monkeypatch):
    """Redirect the downloads store + network layer into a sandbox."""
    import app.services.downloader as dl_mod
    import app.services.url_safety as safety_mod
    import app.webapp.api as api

    root = tmp_path / "downloads"
    monkeypatch.setattr(api, "_dl_root", lambda: root)
    monkeypatch.setattr(dl_mod, "unshorten_url", lambda u: u)
    monkeypatch.setattr(safety_mod, "validate_public_url", lambda u: u)
    return api


class _FakeDownloader:
    """Stands in for MediaDownloader: writes real files, no network."""

    srt_files = ["subs.en.srt"]

    def __init__(self, *a, **kw):
        pass

    def download_video(self, url, target_dir=None):
        p = Path(target_dir) / "abc123_title.mp4"
        p.write_bytes(b"\x00" * 2048)
        return str(p), {"title": "Drama Ep 8", "platform": "douyin"}

    def download_audio(self, url, target_dir=None):
        p = Path(target_dir) / "abc123_title.mp3"
        p.write_bytes(b"\x00" * 1024)
        return str(p), {"title": "Drama Ep 8", "platform": "douyin"}

    def download_subtitles(self, url, target_dir=None, lang="en", allow_ai_fallback=True):
        assert allow_ai_fallback is False, "Downloader SRT must never use the AI fallback"
        out = []
        for name in self.srt_files:
            p = Path(target_dir) / name
            p.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
            out.append(str(p))
        return out, {"title": "Drama Ep 8", "platform": "douyin"}


def _fetch(api, fmt, uid=111, url="https://www.douyin.com/video/123"):
    req = api.DlFetchRequest(url=url, format=fmt)
    return asyncio.run(api.fetch_downloader_format(req, user=_user(uid), _gate=_user(uid)))


def test_fetch_video_stores_file_and_meta(dl_env, monkeypatch):
    import app.services.downloader as dl_mod

    monkeypatch.setattr(dl_mod, "MediaDownloader", _FakeDownloader)
    out = _fetch(dl_env, "video")
    assert out["success"] is True
    assert out["format"] == "video"
    assert out["download_url"] == f"/api/downloader/file/{out['download_id']}"
    assert out["telegram_sendable"] is True
    meta = dl_env._dl_read_meta(out["download_id"])
    assert meta["user_id"] == 111
    assert (dl_env._dl_dir(out["download_id"]) / meta["filename"]).is_file()


def test_fetch_mp3_and_srt(dl_env, monkeypatch):
    import app.services.downloader as dl_mod

    monkeypatch.setattr(dl_mod, "MediaDownloader", _FakeDownloader)
    mp3 = _fetch(dl_env, "mp3")
    assert mp3["filename"].endswith(".mp3")
    srt = _fetch(dl_env, "srt")
    assert srt["filename"].endswith(".srt")
    assert srt["size_bytes"] > 0


def test_fetch_srt_unavailable_is_honest_422(dl_env, monkeypatch):
    import app.services.downloader as dl_mod

    class NoSubs(_FakeDownloader):
        srt_files = []

    monkeypatch.setattr(dl_mod, "MediaDownloader", NoSubs)
    with pytest.raises(HTTPException) as ei:
        _fetch(dl_env, "srt")
    assert ei.value.status_code == 422
    assert ei.value.detail == "no_subtitles_available"
    # No partial download dir is left behind.
    assert list(dl_env._dl_root().iterdir()) == []


def test_fetch_rejects_unknown_format_and_bad_url(dl_env, monkeypatch):
    import app.services.downloader as dl_mod

    monkeypatch.setattr(dl_mod, "MediaDownloader", _FakeDownloader)
    with pytest.raises(HTTPException) as ei:
        _fetch(dl_env, "gif")
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        _fetch(dl_env, "video", url="not-a-url")
    assert ei.value.status_code == 400


def test_download_endpoint_ownership(dl_env, monkeypatch):
    import app.services.downloader as dl_mod
    from app.config import settings

    monkeypatch.setattr(dl_mod, "MediaDownloader", _FakeDownloader)
    out = _fetch(dl_env, "video", uid=111)
    dl_id = out["download_id"]

    # Owner gets the file.
    resp = asyncio.run(dl_env.download_downloader_file(dl_id, user=_user(111)))
    assert Path(resp.path).is_file()
    assert resp.filename == out["filename"]

    # Another user gets 404 (no existence probing).
    with pytest.raises(HTTPException) as ei:
        asyncio.run(dl_env.download_downloader_file(dl_id, user=_user(222)))
    assert ei.value.status_code == 404

    # Admin can fetch anyone's download.
    monkeypatch.setattr(settings, "admin_ids_raw", "999")
    resp = asyncio.run(dl_env.download_downloader_file(dl_id, user=_user(999)))
    assert Path(resp.path).is_file()

    # Malformed / unknown ids 404.
    for bad in ("../etc", "zzzzzzzzzzzz", "a" * 12):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(dl_env.download_downloader_file(bad, user=_user(111)))
        assert ei.value.status_code == 404


def test_download_endpoint_missing_file_404(dl_env, monkeypatch):
    import app.services.downloader as dl_mod

    monkeypatch.setattr(dl_mod, "MediaDownloader", _FakeDownloader)
    out = _fetch(dl_env, "mp3", uid=111)
    meta = dl_env._dl_read_meta(out["download_id"])
    (dl_env._dl_dir(out["download_id"]) / meta["filename"]).unlink()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(dl_env.download_downloader_file(out["download_id"], user=_user(111)))
    assert ei.value.status_code == 404


def test_send_telegram_owner_and_guards(dl_env, monkeypatch):
    import app.services.downloader as dl_mod

    monkeypatch.setattr(dl_mod, "MediaDownloader", _FakeDownloader)
    out = _fetch(dl_env, "video", uid=111)
    dl_id = out["download_id"]

    bot = MagicMock()
    bot.send_video = AsyncMock()
    bot.send_audio = AsyncMock()
    bot.send_document = AsyncMock()
    bot.session.close = AsyncMock()
    with patch("aiogram.Bot", return_value=bot):
        # Non-owner: 404, bot never sends.
        with pytest.raises(HTTPException) as ei:
            asyncio.run(dl_env.send_downloader_to_telegram(dl_id, user=_user(222), _gate=_user(222)))
        assert ei.value.status_code == 404
        bot.send_video.assert_not_called()

        # Owner: video is sent.
        res = asyncio.run(dl_env.send_downloader_to_telegram(dl_id, user=_user(111), _gate=_user(111)))
        assert res["success"] is True
        bot.send_video.assert_awaited_once()


def test_send_telegram_too_large_413(dl_env, monkeypatch):
    import app.services.downloader as dl_mod

    class BigVideo(_FakeDownloader):
        def download_video(self, url, target_dir=None):
            p = Path(target_dir) / "big.mp4"
            with open(p, "wb") as fh:
                fh.truncate(dl_env._DL_TELEGRAM_LIMIT + 1)
            return str(p), {"title": "Big", "platform": "youtube"}

    monkeypatch.setattr(dl_mod, "MediaDownloader", BigVideo)
    out = _fetch(dl_env, "video", uid=111)
    assert out["telegram_sendable"] is False
    with pytest.raises(HTTPException) as ei:
        asyncio.run(dl_env.send_downloader_to_telegram(out["download_id"], user=_user(111), _gate=_user(111)))
    assert ei.value.status_code == 413
    assert ei.value.detail == "file_too_large_telegram"


def test_extract_fallback_does_not_claim_subtitles():
    src = (REPO_ROOT / "app" / "webapp" / "api.py").read_text(encoding="utf-8")
    extract_fn = src.split("async def extract_media_info")[1].split("/recap/upload_video")[0]
    assert '"has_subtitles": True' not in extract_fn
    fallback = extract_fn.split("Graceful fallback response")[1][:800]
    assert '"has_subtitles": False' in fallback


# ── Mini App contracts ────────────────────────────────────────────────

def test_format_cards_wired_not_toast():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
    dl_section = html.split('id="tab-downloader"')[1].split("</section>")[0]
    assert "showStudioToast(t('toast_copied')" not in dl_section
    for el_id in ("btn-dl-fmt-video", "btn-dl-fmt-mp3", "btn-dl-fmt-srt", "dl-delivery-actions"):
        assert f'id="{el_id}"' in dl_section
    assert "dlFetchFormat" in js and "/api/downloader/fetch" in js
    assert "/send-telegram" in js and "no_subtitles_available" in js
    assert "dlSetSrtCardState" in js


def test_downloader_i18n_keys_present_both_languages():
    i18n = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
    for key in ("dl_no_subtitles", "dl_preparing", "dl_ready", "dl_fetch_first",
                "dl_too_large_telegram", "btn_dl_download", "btn_dl_send_telegram"):
        assert i18n.count(f"{key}:") == 2, key
    css = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
    assert ".format-card-unavailable" in css and ".dl-delivery-row" in css


def test_meta_json_shape(dl_env, monkeypatch):
    import app.services.downloader as dl_mod

    monkeypatch.setattr(dl_mod, "MediaDownloader", _FakeDownloader)
    out = _fetch(dl_env, "video", uid=4242)
    raw = json.loads((dl_env._dl_dir(out["download_id"]) / "meta.json").read_text(encoding="utf-8"))
    assert raw["user_id"] == 4242 and raw["format"] == "video"
    assert raw["created_at"] and raw["size_bytes"] == 2048
