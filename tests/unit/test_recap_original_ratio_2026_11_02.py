"""Recap "Original" ratio fix (2026-11-02, branch fix/recap-original-ratio).

Owner-found bug: picking ratio မူရင်း (Original) framed the preview
canvas as 16:9 even for a vertical TikTok source. Investigation
(~/workspace/polish-scratch/recap-ratio-fix-2026-11-02/INVESTIGATION.md)
found three stacked causes, each pinned here:

1. Server: MediaDownloader.get_info() dropped yt-dlp's width/height for
   every remote source (only the local/upload path returned them), so
   the extract payload's width/height were always null and the client's
   state.recapSourceWidth/Height were never set for URL sources.
2. Client: updateCanvasMockupVisuals asserted ratio-16-9 for "Original"
   whenever dimensions were absent.
3. RS-4 honesty: the seek badge painted invented totals for unverified
   sources (the fallback payload's "03:30", or a 60s default), and the
   info card never appeared when the analyze fetch returned nothing.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import downloader as dl_mod
from app.services import url_safety as url_safety_mod
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")

TIKTOK_URL = "https://www.tiktok.com/@voiceclonepro_official/video/7703015166904061192"


# ── 1. get_info carries the source's real dimensions ─────────────────

class _FakeYDL:
    INFO: dict = {}

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return dict(self.INFO)


def _get_info_with(monkeypatch, info, url=TIKTOK_URL):
    monkeypatch.setattr(dl_mod, "unshorten_url", lambda u: u)
    fake = type("FakeYDL", (_FakeYDL,), {"INFO": info})
    monkeypatch.setattr(dl_mod, "yt_dlp", SimpleNamespace(YoutubeDL=fake))
    return dl_mod.MediaDownloader().get_info(url)


def test_get_info_returns_top_level_dimensions(monkeypatch):
    # The exact shape yt-dlp produced live for the owner's TikTok case:
    # top-level 576x1024 (9:16), duration 61 — previously dropped.
    out = _get_info_with(monkeypatch, {
        "title": "Drama Ep1", "duration": 61, "width": 576, "height": 1024,
        "thumbnail": "https://x/y.jpg", "formats": [{"width": 576, "height": 1024, "vcodec": "h264"}],
        "subtitles": {}, "automatic_captions": {},
    })
    assert out["width"] == 576
    assert out["height"] == 1024


def test_get_info_recovers_dimensions_from_formats(monkeypatch):
    # No top-level pair: recover from the largest REAL video format.
    # The storyboard entry is bigger on paper but is an image sheet
    # (vcodec "none") and must not win.
    out = _get_info_with(monkeypatch, {
        "title": "T", "duration": 30, "thumbnail": "",
        "formats": [
            {"width": 1920, "height": 1080, "vcodec": "none", "format_id": "sb0"},
            {"width": 640, "height": 360, "vcodec": "avc1.64001f"},
            {"width": 1280, "height": 720, "vcodec": "avc1.640020"},
            {"width": None, "height": None, "vcodec": "mp4a.40.2"},
        ],
        "subtitles": {}, "automatic_captions": {},
    })
    assert (out["width"], out["height"]) == (1280, 720)


def test_get_info_dimensions_stay_none_when_absent(monkeypatch):
    out = _get_info_with(monkeypatch, {
        "title": "T", "duration": 30, "thumbnail": "",
        "formats": [{"width": None, "height": None, "vcodec": "none"}],
        "subtitles": {}, "automatic_captions": {},
    })
    assert out["width"] is None
    assert out["height"] is None


# ── 2. Extract endpoint: passthrough + fallback shape parity ─────────

@pytest.mark.asyncio
async def test_extract_payload_carries_dimensions(monkeypatch):
    monkeypatch.setattr(dl_mod, "unshorten_url", lambda u: u)
    monkeypatch.setattr(url_safety_mod, "validate_public_url", lambda u: u)

    user = api_mod.User(id=880842, first_name="T", username=None,
                        account_category="FREE", language_code="my")
    req = api_mod.ExtractMediaRequest(url=TIKTOK_URL)

    def fake_get_info(self, url):
        return {"duration": 61, "title": "Drama", "platform": "TikTok",
                "uploader": "voiceclonepro_official", "thumbnail": "",
                "width": 576, "height": 1024, "has_subtitles": False}

    monkeypatch.setattr(dl_mod.MediaDownloader, "get_info", fake_get_info)
    res = await api_mod.extract_media_info(req, user, user)
    assert res["metadata_verified"] is True
    assert (res["width"], res["height"]) == (576, 1024)
    assert res["duration_seconds"] == 61


@pytest.mark.asyncio
async def test_extract_fallback_payload_shape_is_explicit(monkeypatch):
    monkeypatch.setattr(dl_mod, "unshorten_url", lambda u: u)
    monkeypatch.setattr(url_safety_mod, "validate_public_url", lambda u: u)

    user = api_mod.User(id=880843, first_name="T", username=None,
                        account_category="FREE", language_code="my")
    req = api_mod.ExtractMediaRequest(url="https://youtube.com/shorts/g2NWf7CVy3U")

    def boom(self, url):
        raise RuntimeError("ERROR: [youtube] g2NWf7CVy3U: Sign in to confirm")

    monkeypatch.setattr(dl_mod.MediaDownloader, "get_info", boom)
    res = await api_mod.extract_media_info(req, user, user)
    assert res["metadata_verified"] is False
    # Unknown dimensions / unverified duration are explicit keys, so the
    # client never has to guess at missing fields.
    assert res["width"] is None
    assert res["height"] is None
    assert res["duration_seconds"] == 0


# ── 3. Client: "Original" never asserts 16:9 without dimensions ──────

def test_original_ratio_no_longer_hardcodes_169_frame():
    assert 'state.recapRatio === "Original") ratioClass = "ratio-16-9"' not in APP_JS
    # The only remaining rCanvas landscape assignment is the explicit
    # user-chosen 16:9 ratio branch.
    assert APP_JS.count("rCanvas = 16 / 9") == 1
    assert 'if (state.recapRatio === "16:9") rCanvas = 16 / 9;' in APP_JS


def test_original_ratio_still_derives_from_real_dimensions():
    # The known-dimensions derivation is untouched: class from the
    # source aspect + exact inline aspect from the reported dims.
    assert 'if (rSource > 1.3) ratioClass = "ratio-16-9";' in APP_JS
    assert "mockupFrame.style.aspectRatio = `${state.recapSourceWidth} / ${state.recapSourceHeight}`;" in APP_JS
    assert "state.recapSourceWidth = data.width;" in APP_JS


# ── 4. Client: seek badge + info card honesty (RS-4) ─────────────────

def test_seek_badge_never_paints_invented_total():
    # Analyze write is guarded by the verified duration.
    assert "seekBadge.innerText = data.duration_seconds" in APP_JS
    assert '"⏱️ 00:05s / --:--"' in APP_JS
    # updateSeekTimeDisplay: no fabricated 60s default total remains.
    assert "state.recapSourceDuration || 60" not in APP_JS
    assert "state.recapSourceDuration > 0 ? (totalSec || state.recapSourceDuration) : 0" in APP_JS


def test_analyze_catch_paints_unverified_info_card():
    # When the extract returns nothing (timeout/network) the card must
    # appear in the unverified state instead of staying hidden.
    assert "state.lastRecapInfo = { metadata_verified: false, duration_seconds: 0" in APP_JS
    assert "paintRecapInfoMeta(state.lastRecapInfo, false, recapNormalMaxSeconds())" in APP_JS


def test_statics_token_bumped():
    assert INDEX_HTML.count("?v=20261102_rs1") == 3
