"""Release 39 — extraction-boost chain, guarded tikwm fallback,
subtitle plate + letterbox geometry, and the analyze-honesty batch.

Evidence base (measured 2026-11-02, extract-boost-2026-11-02/
REPORT.md): from datacenter IPs YouTube's default yt-dlp client
chain is walled (0/3 on public videos, prod cookies included),
player_client=web_embedded returns the full HD ladder, android/mweb
answer at a 360p ceiling. Antigravity second-opinion applied:
web_embedded is PRIMARY (default/cookies second), android/mweb are
low-res last resorts flagged low_res, tikwm is failure-triggered
only behind a 2.5s timeout + MIME/size assertions + a circuit
breaker, yt-dlp is pinned to the deployed 2026.10.26.

Geometry base (blur-sub-inspect-2026-11-02): the subtitle blur plate
was a 90%-wide hard-edged box and letterboxed renders floated the
subtitle, canvas-sized, at 85% of the padded canvas. The plate is
now a fixed 92%-wide centred band with line-fitted height, and on
letterboxed layouts the font scales to the content box and both
plate and subtitle anchor at the content's lower rim.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

import app.services.downloader as dl_mod
from app.services.downloader import (
    MediaDownloader,
    _flag_low_res,
    _is_blocked_family_error,
    _is_rotation_worthy_error,
    _valid_tikwm_play_url,
    classify_source_error,
)

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
REQUIREMENTS = (ROOT / "requirements.txt").read_text(encoding="utf-8")
PIPELINE_PY = (ROOT / "app/services/recap_engine/recap_pipeline.py").read_text(encoding="utf-8")

YOUTUBE_URL = "https://www.youtube.com/watch?v=g2NWf7CVy3U"
TIKTOK_URL = "https://www.tiktok.com/@someone/video/7391505677920816389"
GTV_URL = "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerJoyrides.mp4"

SIGNIN_ERR = "ERROR: [youtube] g2NWf7CVy3U: Sign in to confirm you're not a bot. Use --cookies-from-browser or --cookies for the authentication"
PLAYER_ERR = "ERROR: [youtube] g2NWf7CVy3U: Failed to extract any player response; please report this issue on https://github.com/yt-dlp/yt-dlp/issues"
PRIVATE_ERR = "ERROR: [youtube] abc123: This video is private"
RAW_IP_BLOCK = "ERROR: [TikTok] 7391505677920816389: Your IP address is blocked from accessing this post"
SERVER_CLASSIFIED_TIKTOK = (
    "⚠️ TikTok က ကျွန်တော်တို့ ဆာဗာမှ ဗီဒီယို ဆွဲယူခြင်းကို ပိတ်ထားပါသည်။ "
    "ဗီဒီယိုဖိုင်ကို Mini App ရှိ '📁 Video Upload' မှ တိုက်ရိုက် တင်၍ "
    "ပြုလုပ်နိုင်ပါသည် ခင်ဗျာ။"
)


@pytest.fixture(autouse=True)
def _reset_tikwm_breaker():
    dl_mod._TIKWM_BREAKER.reset()
    yield
    dl_mod._TIKWM_BREAKER.reset()


def _client_of(opts) -> str:
    ea = (opts.get("extractor_args") or {}).get("youtube") or {}
    pc = ea.get("player_client")
    return pc[0] if pc else "default"


class _ChainYDL:
    """yt_dlp.YoutubeDL stand-in driven by a per-test handler.

    handler(opts, url, download) -> info dict (or raises)."""

    calls: list = []
    handler = None

    def __init__(self, opts):
        self.opts = opts
        type(self).calls.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        return type(self).handler(self.opts, url, download)

    def prepare_filename(self, info):
        return info["_prepared_path"]


def _patch_chain(monkeypatch, handler):
    _ChainYDL.calls = []
    _ChainYDL.handler = handler
    monkeypatch.setattr(dl_mod, "unshorten_url", lambda u: u)
    monkeypatch.setattr(dl_mod.yt_dlp, "YoutubeDL", _ChainYDL)
    return _ChainYDL


def _info(formats=None, **kw):
    base = {
        "id": "vid1",
        "title": "A Video",
        "duration": 42,
        "uploader": "Chan",
        "thumbnail": None,
        "subtitles": {},
        "automatic_captions": {},
        "formats": formats if formats is not None else [
            {"vcodec": "avc1", "acodec": "mp4a", "width": 1080, "height": 1080},
        ],
    }
    base.update(kw)
    return base


# ── 1. Chain order + fail-fast (info stage) ─────────────────────────

def test_chain_web_embedded_is_primary_and_wins(monkeypatch, tmp_path):
    def handler(opts, url, download):
        assert _client_of(opts) == "web_embedded"  # any other client reached = order broken
        return _info()

    _patch_chain(monkeypatch, handler)
    dl = MediaDownloader(download_dir=str(tmp_path))
    out = dl.get_info(YOUTUBE_URL)
    assert out["title"] == "A Video"
    assert out["low_res"] is False
    assert len(_ChainYDL.calls) == 1
    opts = _ChainYDL.calls[0]
    # Fail-fast info stage (R39.1 trim) + bounded per-attempt timeout.
    assert opts["retries"] == 1
    assert opts["fragment_retries"] == 1
    assert opts["extractor_retries"] == 1
    assert opts["socket_timeout"] == 10


def test_chain_rotates_in_order_and_flags_low_res(monkeypatch, tmp_path):
    seen = []

    def handler(opts, url, download):
        client = _client_of(opts)
        seen.append(client)
        if client == "web_embedded":
            raise Exception(SIGNIN_ERR)
        if client == "default":
            raise Exception(PLAYER_ERR)
        # android rescue: progressive 360p only
        return _info(formats=[{"vcodec": "avc1", "acodec": "mp4a", "width": 640, "height": 360}])

    _patch_chain(monkeypatch, handler)
    dl = MediaDownloader(download_dir=str(tmp_path))
    out = dl.get_info(YOUTUBE_URL)
    assert seen == ["web_embedded", "default", "android"]
    assert out["low_res"] is True
    # The cookie'd default attempt carries no player_client override.
    assert "extractor_args" not in _ChainYDL.calls[1]


def test_chain_stops_on_private_video(monkeypatch, tmp_path):
    def handler(opts, url, download):
        raise Exception(PRIVATE_ERR)

    _patch_chain(monkeypatch, handler)
    dl = MediaDownloader(download_dir=str(tmp_path))
    with pytest.raises(RuntimeError) as ei:
        dl.get_info(YOUTUBE_URL)
    assert len(_ChainYDL.calls) == 1  # definitive truth: no rotation
    # Classified ⚠️ copy, never the raw extractor string. (The
    # classifier's unavailable branch keys on "private video"; the
    # "This video is private" phrasing lands in its bracketed
    # catch-all — pre-existing R38 behaviour, unchanged here.)
    assert str(ei.value).startswith("⚠️")
    assert "This video is private" not in str(ei.value)


def test_chain_non_youtube_behaviour_unchanged(monkeypatch, tmp_path):
    def handler(opts, url, download):
        raise Exception("some brand-new failure mode")

    _patch_chain(monkeypatch, handler)
    dl = MediaDownloader(download_dir=str(tmp_path))
    with pytest.raises(Exception, match="brand-new failure"):
        dl.get_info(TIKTOK_URL)
    assert len(_ChainYDL.calls) == 1
    opts = _ChainYDL.calls[0]
    assert "extractor_args" not in opts
    assert opts["retries"] == 10  # base opts untouched for other hosts


def test_rotation_worthy_classification():
    assert _is_rotation_worthy_error(SIGNIN_ERR)
    assert _is_rotation_worthy_error(PLAYER_ERR)
    assert _is_rotation_worthy_error("ERROR: No video formats found!")
    assert not _is_rotation_worthy_error(PRIVATE_ERR)
    assert not _is_rotation_worthy_error("ERROR: Video unavailable")
    assert not _is_rotation_worthy_error("ERROR: Unsupported URL")
    assert not _is_rotation_worthy_error("ERROR: This video has been removed by the uploader")


def test_low_res_flag_helper():
    info = {"formats": [{"vcodec": "avc1", "height": 360}]}
    assert _flag_low_res(info, YOUTUBE_URL, "android") is True
    assert info["low_res"] is True
    info2 = {"formats": [{"vcodec": "avc1", "height": 720}]}
    assert _flag_low_res(info2, YOUTUBE_URL, "web_embedded") is False
    assert info2["low_res"] is False
    info3 = {"formats": [{"vcodec": "none", "height": 1080}]}  # audio-only entries don't count
    assert _flag_low_res(info3, YOUTUBE_URL, "android") is False


# ── 2. Chain in the media paths ─────────────────────────────────────

def test_download_video_chain_media_retries_and_success(monkeypatch, tmp_path):
    dest = tmp_path / "dl"
    dest.mkdir()

    def handler(opts, url, download):
        client = _client_of(opts)
        if client in ("web_embedded", "default"):
            raise Exception(SIGNIN_ERR)
        fp = dest / "vid1_title.mp4"
        fp.write_bytes(b"x" * 2048)
        return _info(_prepared_path=str(fp))

    _patch_chain(monkeypatch, handler)
    dl = MediaDownloader(download_dir=str(tmp_path))
    file_path, info = dl.download_video(YOUTUBE_URL, target_dir=str(dest))
    assert file_path.endswith(".mp4")
    assert len(_ChainYDL.calls) == 3
    # Media stage keeps the retries:10 byte policy + bounded timeout.
    assert _ChainYDL.calls[0]["retries"] == 10
    assert _ChainYDL.calls[0]["socket_timeout"] == 20


def test_download_video_all_clients_fail_then_format_fallback(monkeypatch, tmp_path):
    def handler(opts, url, download):
        raise Exception(SIGNIN_ERR)

    _patch_chain(monkeypatch, handler)
    dl = MediaDownloader(download_dir=str(tmp_path))
    with pytest.raises(RuntimeError) as ei:
        dl.download_video(YOUTUBE_URL, target_dir=str(tmp_path))
    # 4 chain attempts + 1 resilient format fallback on the last opts.
    assert len(_ChainYDL.calls) == 5
    fb = _ChainYDL.calls[-1]
    assert fb["format"] == "bestvideo+bestaudio/best"
    assert _client_of(fb) == "mweb"
    assert "Anti-Bot" in str(ei.value)  # legacy sign-in copy, unchanged


def test_download_audio_chain(monkeypatch, tmp_path):
    dest = tmp_path / "au"
    dest.mkdir()

    def handler(opts, url, download):
        if _client_of(opts) == "web_embedded":
            raise Exception(PLAYER_ERR)
        base = dest / "vid1_title.webm"
        (dest / "vid1_title.mp3").write_bytes(b"x" * 1024)
        return _info(_prepared_path=str(base))

    _patch_chain(monkeypatch, handler)
    dl = MediaDownloader(download_dir=str(tmp_path))
    mp3_path, info = dl.download_audio(YOUTUBE_URL, target_dir=str(dest))
    assert mp3_path.endswith(".mp3")
    assert len(_ChainYDL.calls) == 2


def test_download_audio_for_transcription_chain(monkeypatch, tmp_path):
    dest = tmp_path / "tr"
    dest.mkdir()

    def handler(opts, url, download):
        if _client_of(opts) != "android":
            raise Exception(SIGNIN_ERR)
        fp = dest / "transcribe_vid1.m4a"
        fp.write_bytes(b"x" * 512)
        return _info(_prepared_path=str(fp))

    _patch_chain(monkeypatch, handler)
    dl = MediaDownloader(download_dir=str(tmp_path))
    out = dl.download_audio_for_transcription(YOUTUBE_URL, target_dir=str(dest))
    assert out.endswith(".m4a")
    assert len(_ChainYDL.calls) == 3


# ── 3. tikwm fallback guardrails ────────────────────────────────────

class _Resp:
    def __init__(self, *, json_data=None, status_code=200, headers=None, chunks=()):
        self._json = json_data
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._json

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, chunk_size=1):
        yield from self._chunks


PLAY_URL = "https://v16-webapp.tiktokcdn-us.com/abc/video.mp4"
TIKWM_PAYLOAD = {
    "code": 0,
    "data": {
        "id": "7391505677920816389",
        "title": "Blocked post",
        "author": {"nickname": "Creator"},
        "duration": 33,
        "cover": "https://p16.tiktokcdn-us.com/cover.jpg",
        "play": PLAY_URL,
    },
}


def _tikwm_dispatcher(api_resp=None, probe_resp=None, media_resp=None, record=None):
    def _get(url, **kwargs):
        if record is not None:
            record.append((url, kwargs))
        if url == dl_mod.TIKWM_API_URL:
            if isinstance(api_resp, Exception):
                raise api_resp
            return api_resp
        if "Range" in (kwargs.get("headers") or {}):
            if isinstance(probe_resp, Exception):
                raise probe_resp
            return probe_resp
        if isinstance(media_resp, Exception):
            raise media_resp
        return media_resp

    return _get


def _good_probe():
    return _Resp(status_code=206, headers={"Content-Type": "video/mp4", "Content-Range": "bytes 0-0/2000000"})


def _patch_tiktok_blocked(monkeypatch):
    def handler(opts, url, download):
        raise Exception(RAW_IP_BLOCK)

    _patch_chain(monkeypatch, handler)


def test_tikwm_fallback_resolves_blocked_tiktok(monkeypatch, tmp_path):
    _patch_tiktok_blocked(monkeypatch)
    record = []
    monkeypatch.setattr(
        dl_mod.requests, "get",
        _tikwm_dispatcher(api_resp=_Resp(json_data=TIKWM_PAYLOAD), probe_resp=_good_probe(), record=record),
    )
    dl = MediaDownloader(download_dir=str(tmp_path))
    out = dl.get_info(TIKTOK_URL)
    assert out["tikwm_fallback"] is True
    assert out["title"] == "Blocked post"
    assert out["duration"] == 33
    assert out["filesize"] == 2000000
    api_kwargs = record[0][1]
    assert api_kwargs["timeout"] == dl_mod.TIKWM_TIMEOUT == 2.5  # hard timeout on the resolver call
    probe_kwargs = record[1][1]
    assert probe_kwargs["timeout"] == 2.5


def test_tikwm_html_probe_rejected_falls_back_to_classified(monkeypatch, tmp_path):
    _patch_tiktok_blocked(monkeypatch)
    monkeypatch.setattr(
        dl_mod.requests, "get",
        _tikwm_dispatcher(
            api_resp=_Resp(json_data=TIKWM_PAYLOAD),
            probe_resp=_Resp(status_code=200, headers={"Content-Type": "text/html; charset=utf-8"}),
        ),
    )
    dl = MediaDownloader(download_dir=str(tmp_path))
    with pytest.raises(RuntimeError) as ei:
        dl.get_info(TIKTOK_URL)
    assert str(ei.value) == SERVER_CLASSIFIED_TIKTOK  # fallback failure = existing classified error


def test_tikwm_evil_play_host_rejected(monkeypatch, tmp_path):
    _patch_tiktok_blocked(monkeypatch)
    payload = {"code": 0, "data": dict(TIKWM_PAYLOAD["data"], play="https://evil.example.com/x.mp4")}
    monkeypatch.setattr(
        dl_mod.requests, "get",
        _tikwm_dispatcher(api_resp=_Resp(json_data=payload), probe_resp=_good_probe()),
    )
    dl = MediaDownloader(download_dir=str(tmp_path))
    with pytest.raises(RuntimeError) as ei:
        dl.get_info(TIKTOK_URL)
    assert str(ei.value) == SERVER_CLASSIFIED_TIKTOK


def test_tikwm_circuit_breaker_opens_and_skips(monkeypatch, tmp_path):
    calls = []

    def _get(url, **kwargs):
        calls.append(url)
        raise ConnectionError("resolver down")

    monkeypatch.setattr(dl_mod.requests, "get", _get)
    dl = MediaDownloader(download_dir=str(tmp_path))
    for _ in range(dl_mod.TIKWM_BREAKER_THRESHOLD):
        assert dl._tikwm_resolve(TIKTOK_URL) is None
    assert len(calls) == dl_mod.TIKWM_BREAKER_THRESHOLD
    assert dl_mod._TIKWM_BREAKER.allow() is False
    # Open breaker: the resolver is not even called.
    assert dl._tikwm_resolve(TIKTOK_URL) is None
    assert len(calls) == dl_mod.TIKWM_BREAKER_THRESHOLD


def test_tikwm_not_triggered_by_other_failures(monkeypatch, tmp_path):
    def handler(opts, url, download):
        raise Exception("ERROR: [TikTok] 1: No video formats found!")

    _patch_chain(monkeypatch, handler)
    called = []
    monkeypatch.setattr(dl_mod.requests, "get", lambda *a, **k: called.append(a) or _Resp())
    dl = MediaDownloader(download_dir=str(tmp_path))
    with pytest.raises(RuntimeError):
        dl.get_info(TIKTOK_URL)
    assert called == []  # 'No video formats' is the unavailable family, not the blocked family
    assert not _is_blocked_family_error("ERROR: [TikTok] 1: No video formats found!")
    assert _is_blocked_family_error(RAW_IP_BLOCK)
    assert _is_blocked_family_error("ERROR: HTTP Error 403: Forbidden")


def test_tikwm_play_url_validation():
    assert _valid_tikwm_play_url(PLAY_URL)
    assert _valid_tikwm_play_url("https://api16-normal-c-useast1a.tiktokv.com/aweme/v1/play/?x=1")
    assert not _valid_tikwm_play_url("http://v16.tiktokcdn-us.com/x.mp4")  # https only
    assert not _valid_tikwm_play_url("https://tiktokcdn-us.com.evil.com/x.mp4")
    assert not _valid_tikwm_play_url("https://example.com/x.mp4")
    assert not _valid_tikwm_play_url(None)
    assert not _valid_tikwm_play_url(12345)


def test_tikwm_download_refuses_non_mp4_stream(monkeypatch, tmp_path):
    dest = tmp_path / "tk"
    dest.mkdir()
    monkeypatch.setattr(
        dl_mod.requests, "get",
        _tikwm_dispatcher(
            api_resp=_Resp(json_data=TIKWM_PAYLOAD),
            probe_resp=_good_probe(),
            media_resp=_Resp(status_code=200, headers={"Content-Type": "text/html"}, chunks=[b"<html>nope</html>"]),
        ),
    )
    dl = MediaDownloader(download_dir=str(tmp_path))
    assert dl._tikwm_download_video(TIKTOK_URL, TIKTOK_URL, str(dest)) is None
    assert list(dest.iterdir()) == []  # nothing trusted, nothing left behind


def test_tikwm_download_success(monkeypatch, tmp_path):
    dest = tmp_path / "tk2"
    dest.mkdir()
    body = b"\x00" * 20000
    monkeypatch.setattr(
        dl_mod.requests, "get",
        _tikwm_dispatcher(
            api_resp=_Resp(json_data=TIKWM_PAYLOAD),
            probe_resp=_good_probe(),
            media_resp=_Resp(status_code=200, headers={"Content-Type": "video/mp4"}, chunks=[body]),
        ),
    )
    dl = MediaDownloader(download_dir=str(tmp_path))
    out = dl._tikwm_download_video(TIKTOK_URL, TIKTOK_URL, str(dest))
    assert out is not None
    file_path, info = out
    assert Path(file_path).stat().st_size == 20000
    assert info["tikwm_fallback"] is True


# ── 4. B: direct-host refusal classification ────────────────────────

def test_classify_direct_host_refusal_403():
    msg = classify_source_error("ERROR: [generic] Unable to download webpage: HTTP Error 403: Forbidden", GTV_URL)
    assert msg is not None and msg.startswith("⚠️")
    assert "ငြင်းပယ်ထားပါသည်" in msg  # host refused — distinct from the platform-block copy
    assert "ပိတ်ထားပါသည်" not in msg
    assert "Upload" in msg  # direct-upload guidance present


def test_classify_direct_host_refusal_401():
    msg = classify_source_error("ERROR: [generic] HTTP Error 401: Unauthorized", "https://files.example.com/movie.mp4")
    assert msg is not None and "ငြင်းပယ်ထားပါသည်" in msg


def test_classify_share_site_403_keeps_platform_block_copy():
    # Not a direct media file → the pre-existing blocked branch owns it.
    msg = classify_source_error("ERROR: [generic] share?id=0: Unable to download webpage: HTTP Error 403: Forbidden", "https://vidmate.example.com/s/x")
    assert msg is not None and "ပိတ်ထားပါသည်" in msg and "ငြင်းပယ်ထားပါသည်" not in msg


def test_classify_direct_mp4_other_errors_unaffected():
    # A direct file with a network timeout keeps the network copy.
    msg = classify_source_error("ERROR: Unable to download webpage: <urlopen error timed out>", GTV_URL)
    assert msg is not None and "ကွန်ရက်" in msg


# ── 5. Plate + letterbox geometry ───────────────────────────────────

from app.services.recap_engine.core import (  # noqa: E402
    content_rim_y,
    frame_layout,
    subtitle_font_px,
    subtitle_plate_box,
    write_synced_ass_file,
)

LETTERBOX = None  # built in the fixture-free helper below


def _letterbox_layout():
    return frame_layout("9:16", "blur", 1920, 1080)


def _ass_style_fields(path: Path):
    line = next(l for l in path.read_text(encoding="utf-8").splitlines() if l.startswith("Style:"))
    parts = [p.strip() for p in line.split(",")]
    return {"font_size": int(parts[2]), "margin_v": int(parts[-2])}


def test_letterbox_layout_facts():
    lay = _letterbox_layout()
    assert (lay["cw"], lay["ch"]) == (1080, 1920)
    assert (lay["pw"], lay["ph"], lay["oy"]) == (1080, 608, 656)
    assert content_rim_y(lay, 1080, 1920) == 1264
    full = frame_layout("9:16", "blur", 1080, 1920)
    assert content_rim_y(full, 1080, 1920) is None


def test_font_px_full_frame_formula_unchanged():
    assert subtitle_font_px(1080, 1920, 16, 1, None) == 44
    assert subtitle_font_px(1920, 1080, 16, 1, None) == 36
    full = frame_layout("9:16", "blur", 1080, 1920)
    assert subtitle_font_px(1080, 1920, 16, 1, full) == 44


def test_font_px_letterbox_scales_to_content_box():
    lay = _letterbox_layout()
    # min(content)=608 vs min(canvas)=1080 → 44.8 × 608/1080 ≈ 25
    assert subtitle_font_px(1080, 1920, 16, 1, lay) == 25
    assert subtitle_font_px(1080, 1920, 16, 2, lay) == 37


def test_plate_full_frame_92_percent_and_safe_margin():
    plate = subtitle_plate_box(1080, 1920, None, raw_font_size=16, sub_lines=1, sub_y=0.85, min_h=0.07)
    assert plate == {"x": 0.04, "y": 0.85, "w": 0.92, "h": 0.07}
    # A centre pushed to the canvas edge is pulled back so the plate
    # bottom respects the ~8% safe margin (bottom == 0.92).
    plate2 = subtitle_plate_box(1080, 1920, None, raw_font_size=16, sub_lines=1, sub_y=0.98, min_h=0.07)
    assert abs((plate2["y"] + plate2["h"] / 2) - 0.92) < 1e-9


def test_plate_height_grows_for_two_lines():
    plate = subtitle_plate_box(1080, 1920, None, raw_font_size=16, sub_lines=2, sub_y=0.85, min_h=0.07)
    assert plate["h"] > 0.07  # settle-fit: the plate grows to fit the wrapped lines
    assert abs(plate["h"] - 0.0904) < 0.001


def test_plate_letterbox_anchors_at_content_rim():
    lay = _letterbox_layout()
    plate = subtitle_plate_box(1080, 1920, lay, raw_font_size=16, sub_lines=1, sub_y=0.85, min_h=0.07)
    assert plate["x"] == 0.04 and plate["w"] == 0.92
    centre_px = plate["y"] * 1920
    h_px = plate["h"] * 1920
    rim = 1264
    # The plate straddles the rim: top above it, bottom below it,
    # centre a quarter plate-height above it.
    assert centre_px - h_px / 2 < rim < centre_px + h_px / 2
    assert abs(centre_px - (rim - h_px / 4)) < 1.5
    # Pure fit (no caller minimum) is much shorter than the legacy 7%.
    fit = subtitle_plate_box(1080, 1920, lay, raw_font_size=16, sub_lines=1, sub_y=0.85, min_h=None)
    assert fit["h"] < 0.03


def test_ass_legacy_geometry_without_layout(tmp_path):
    out = write_synced_ass_file(
        [(0.0, 3.0, "မင်္ဂလာပါ")], tmp_path / "a.ass", 1080, 1920,
        raw_font_size=16, sub_lines=1, sub_y=0.85,
    )
    fields = _ass_style_fields(out)
    assert fields["font_size"] == 44
    assert fields["margin_v"] == 263


def test_ass_letterbox_geometry(tmp_path):
    lay = _letterbox_layout()
    out = write_synced_ass_file(
        [(0.0, 3.0, "မင်္ဂလာပါ")], tmp_path / "b.ass", 1080, 1920,
        raw_font_size=16, sub_lines=1, sub_y=0.85, layout=lay,
    )
    fields = _ass_style_fields(out)
    assert fields["font_size"] == 25
    # MarginV 649 → text bottom at 1920-649=1271 ≈ rim 1264 (+text_h/4 overlap)
    assert fields["margin_v"] == 649
    out2 = write_synced_ass_file(
        [(0.0, 3.0, "မင်္ဂလာပါ")], tmp_path / "c.ass", 1080, 1920,
        raw_font_size=16, sub_lines=2, sub_y=0.85, layout=lay,
    )
    fields2 = _ass_style_fields(out2)
    assert fields2["font_size"] == 37
    assert fields2["margin_v"] == 635


def test_pipeline_uses_plate_helper():
    assert "subtitle_plate_box(" in PIPELINE_PY


# ── 6. SRT fallback path: rim anchor (real ffmpeg render) ──────────

FFMPEG = shutil.which("ffmpeg")


def _render_srt_case(tmp_path: Path, layout: dict) -> Path:
    from PIL import Image  # noqa: F401  (presence check mirrors shaping tests)
    from app.services.recap_engine.core import apply_production_overlays

    src = tmp_path / "src.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=black:size=1080x1920:rate=30:duration=2", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )
    srt = tmp_path / "subs.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nမင်္ဂလာပါ စမ်းသပ်စာသား\n", encoding="utf-8")
    out = tmp_path / "out.mp4"
    apply_production_overlays(
        FFMPEG, src, out,
        {"subtitles_file": str(srt), "sub_y": 0.85, "sub_lines": 1,
         "subtitles_font_size": 16, "sub_style": "yellow"},
        "libx264", tmp_path / "w", frame_size=(1080, 1920), layout=layout,
    )
    frame = tmp_path / "f.png"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", "1.0",
         "-i", str(out), "-frames:v", "1", str(frame)],
        check=True, capture_output=True,
    )
    return frame


def _band_bright(frame: Path, y0: int, y1: int) -> int:
    from PIL import Image

    img = Image.open(frame).convert("L").crop((140, y0, 940, y1))
    return sum(1 for v in img.getdata() if v > 120)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_srt_letterbox_subtitle_rides_the_rim(tmp_path):
    layout = {"pw": 1080, "ph": 608, "ox": 0, "oy": 656, "sw": 1920, "sh": 1080, "fill": "blur"}
    frame = _render_srt_case(tmp_path, layout)
    rim_band = _band_bright(frame, 1150, 1380)     # around the content rim (1264)
    legacy_band = _band_bright(frame, 1500, 1750)  # the old 85%-of-canvas home
    assert rim_band > 400
    assert legacy_band < max(50, rim_band // 4)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_srt_full_frame_subtitle_stays_low(tmp_path):
    layout = {"pw": 1080, "ph": 1920, "ox": 0, "oy": 0, "sw": 1080, "sh": 1920, "fill": "blur"}
    frame = _render_srt_case(tmp_path, layout)
    rim_band = _band_bright(frame, 1150, 1380)
    legacy_band = _band_bright(frame, 1500, 1750)
    assert legacy_band > 400
    assert rim_band < max(50, legacy_band // 4)


# ── 7. A + C: client honesty + size format (node) ──────────────────

def _run_node(script_body: str, tmp_name: str) -> dict:
    import json

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    tmp = ROOT / "tests/unit" / tmp_name
    try:
        tmp.write_text(script_body, encoding="utf-8")
        res = subprocess.run([node, str(tmp)], capture_output=True, text=True, timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_format_dl_size_units():
    start = APP_JS.index("function formatDlSize")
    end = APP_JS.index("function dlRenderDeliveryRow")
    driver = r"""
const out = {
  kb32: formatDlSize(32768),
  b500: formatDlSize(500),
  mb1: formatDlSize(1048576),
  mb15: formatDlSize(1572864),
  gb2: formatDlSize(2147483648),
  unknown: formatDlSize(null),
  zero: formatDlSize(0),
};
console.log(JSON.stringify(out));
"""
    out = _run_node(APP_JS[start:end] + "\n" + driver, "_r39_dlsize_tmp.js")
    assert out["kb32"] == "32 KB"
    assert out["b500"] == "500 B"
    assert out["mb1"] == "1.0 MB"
    assert out["mb15"] == "1.5 MB"
    assert out["gb2"] == "2.00 GB"
    assert out["unknown"] == "?"
    assert out["zero"] == "0 B"


def test_blocked_link_info_recognises_host_refusal_copy():
    start = APP_JS.index("function mapErrorToToast")
    end = APP_JS.index("function showStudioInputModal")
    refusal = (
        "⚠️ ဒီ ဗီဒီယိုဖိုင်၏ မူရင်း ဆာဗာ (host) က ကျွန်တော်တို့ ဆာဗာမှ ဆွဲယူခြင်းကို "
        "ငြင်းပယ်ထားပါသည်။ အခြား အရင်းအမြစ် လင့်ခ် ဖြင့် စမ်းကြည့်ပါ။"
    )
    import json as _json

    driver = (
        "const REFUSAL = " + _json.dumps(refusal) + ";\n"
        "const out = { my: {}, en: {} };\n"
        "for (const lang of ['my', 'en']) {\n"
        "  currentLang = lang;\n"
        "  out[lang].blocked = blockedLinkInfo(REFUSAL).blocked;\n"
        "  out[lang].toast = mapErrorToToast(REFUSAL, 'recap');\n"
        "  out[lang].generic = t('err_recap_failed');\n"
        "}\n"
        "console.log(JSON.stringify(out));\n"
    )
    script = (
        "var window = {};\n"
        "var localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };\n"
        + I18N_JS + "\n" + APP_JS[start:end] + "\n" + driver
    )
    out = _run_node(script, "_r39_refusal_tmp.js")
    for lang in ("my", "en"):
        assert out[lang]["blocked"] is True
        assert out[lang]["toast"] != out[lang]["generic"]


def test_analyze_honesty_wiring_present():
    # Warning element by the CTA + painter/clearer helpers + call sites.
    assert 'id="recap-unverified-warning"' in INDEX_HTML
    assert "function paintRecapUnverifiedWarning(" in APP_JS
    assert "function clearRecapInfoCard(" in APP_JS
    assert "clearRecapInfoCard();" in APP_JS  # Analyze start clears the stale card
    assert APP_JS.count("paintRecapUnverifiedWarning();") >= 4
    assert I18N_JS.count("recap_unverified_warning") == 2  # my + en
    # The warning is advisory: the painter never touches the CTA.
    start = APP_JS.index("function paintRecapUnverifiedWarning(")
    end = APP_JS.index("window.paintRecapUnverifiedWarning", start)
    assert "btn-start-recap" not in APP_JS[start:end]
    assert "disabled" not in APP_JS[start:end]


# ── 8. yt-dlp pin ───────────────────────────────────────────────────

def test_yt_dlp_pinned_to_deployed_version():
    lines = [l.strip() for l in REQUIREMENTS.splitlines() if l.strip().startswith("yt-dlp")]
    assert lines == ["yt-dlp==2026.10.26"]


# ── 9. R39.1 info-stage latency budget ──────────────────────────────

def test_info_stage_budget_all_clients_and_media_unchanged(tmp_path):
    """R39.1 trim: EVERY info-stage plan carries the bounded budget
    (retries/fragment/extractor 1, socket cap 10s) so a fully-walled
    YouTube URL fails the whole 4-client chain in ≤ ~40s; the media
    stage keeps the byte budgets (retries 10, socket cap 20s)
    exactly as R39 left them."""
    dl = MediaDownloader(download_dir=str(tmp_path))
    base = dl._get_base_opts(YOUTUBE_URL)

    info_plans = list(dl._attempt_plans(base, YOUTUBE_URL, stage="info"))
    assert [label for label, _ in info_plans] == ["web_embedded", "default", "android", "mweb"]
    for _label, opts in info_plans:
        assert opts["retries"] == 1
        assert opts["fragment_retries"] == 1
        assert opts["extractor_retries"] == 1
        assert opts["socket_timeout"] == 10

    media_plans = list(dl._attempt_plans(base, YOUTUBE_URL, stage="media"))
    assert [label for label, _ in media_plans] == ["web_embedded", "default", "android", "mweb"]
    for _label, opts in media_plans:
        assert opts["retries"] == 10
        assert opts["socket_timeout"] == 20

    # The budget constants themselves are the contract.
    assert dl_mod.INFO_STAGE_RETRIES == 1
    assert dl_mod.INFO_STAGE_SOCKET_TIMEOUT == 10
    assert dl_mod.YOUTUBE_ATTEMPT_SOCKET_TIMEOUT == 20
