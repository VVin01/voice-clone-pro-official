"""Orientation-aware download & local-scale resolution cap tests (2026-11-16).

Bug root cause: recap download cap (``max_height = 1080`` for VIP/admin,
``720`` for FREE/preview) filtered exclusively on ``height <= cap`` in the
primary yt-dlp clauses and ``scale=-2:'min(cap,ih)'`` in
``process_local_video``. For vertical sources (YouTube Shorts, TikTok,
Reels) height is the LONG side, so a 1080x1920 Shorts video was capped to
480x854 (VIP) or 360x640 (FREE), and 1080x1920 uploads were shrunk to
608x1080 / 404x720.

Pinned here:
1. Canonical vertical format fixture (144x256 .. 2160x3840, avc1 MP4 at
   480x854/720x1280/1080x1920, vp9/av01 at 608x1080):
   - cap 1080 -> 1080x1920 (avc1 MP4 + m4a)
   - cap 720  -> 720x1280 (avc1 MP4 + m4a)
2. Portrait codec/container fallback ladder (no avc1, pre-merged only,
   no m4a audio) still picks width <= cap.
3. Landscape fixture (256x144 .. 3840x2160): unchanged winners
   (1920x1080 at cap 1080, 1280x720 at cap 720).
4. Local-file scaling for portrait & landscape: no scale when under/at
   cap (never upscale), downscales on the short axis when over cap.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yt_dlp

import app.services.downloader as dl_mod
from app.services.downloader import (
    MediaDownloader,
    _build_video_format_spec,
    _local_video_scale_filter,
    probe_local_media,
)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


# ── Fixture builders ──────────────────────────────────────────────────

def _audio_formats():
    return [
        {
            "format_id": "251",
            "ext": "webm",
            "vcodec": "none",
            "acodec": "opus",
            "abr": 160,
            "tbr": 160,
            "url": "https://cdn.example/251.webm",
        },
        {
            "format_id": "140",
            "ext": "m4a",
            "vcodec": "none",
            "acodec": "mp4a.40.2",
            "abr": 128,
            "tbr": 128,
            "url": "https://cdn.example/140.m4a",
        },
    ]


def _canonical_vertical_formats():
    """Mirror of canonical YouTube Shorts ladder (144x256 .. 2160x3840)."""
    return [
        *_audio_formats(),
        {"format_id": "160", "ext": "mp4", "vcodec": "avc1.4d400c", "acodec": "none", "width": 144, "height": 256, "tbr": 110, "url": "https://cdn.example/160.mp4"},
        {"format_id": "278", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 144, "height": 256, "tbr": 95, "url": "https://cdn.example/278.webm"},
        {"format_id": "394", "ext": "mp4", "vcodec": "av01.0.00M.08", "acodec": "none", "width": 144, "height": 256, "tbr": 85, "url": "https://cdn.example/394.mp4"},
        {"format_id": "133", "ext": "mp4", "vcodec": "avc1.4d4015", "acodec": "none", "width": 240, "height": 426, "tbr": 240, "url": "https://cdn.example/133.mp4"},
        {"format_id": "242", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 240, "height": 426, "tbr": 210, "url": "https://cdn.example/242.webm"},
        {"format_id": "395", "ext": "mp4", "vcodec": "av01.0.00M.08", "acodec": "none", "width": 240, "height": 426, "tbr": 180, "url": "https://cdn.example/395.mp4"},
        {"format_id": "134", "ext": "mp4", "vcodec": "avc1.4d401e", "acodec": "none", "width": 360, "height": 640, "tbr": 520, "url": "https://cdn.example/134.mp4"},
        {"format_id": "18", "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "width": 360, "height": 640, "tbr": 580, "url": "https://cdn.example/18.mp4"},
        {"format_id": "243", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 360, "height": 640, "tbr": 420, "url": "https://cdn.example/243.webm"},
        {"format_id": "396", "ext": "mp4", "vcodec": "av01.0.01M.08", "acodec": "none", "width": 360, "height": 640, "tbr": 360, "url": "https://cdn.example/396.mp4"},
        {"format_id": "135", "ext": "mp4", "vcodec": "avc1.4d401f", "acodec": "none", "width": 480, "height": 854, "tbr": 950, "url": "https://cdn.example/135.mp4"},
        {"format_id": "244", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 480, "height": 854, "tbr": 760, "url": "https://cdn.example/244.webm"},
        {"format_id": "397", "ext": "mp4", "vcodec": "av01.0.04M.08", "acodec": "none", "width": 480, "height": 854, "tbr": 640, "url": "https://cdn.example/397.mp4"},
        {"format_id": "247_608", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 608, "height": 1080, "tbr": 1200, "url": "https://cdn.example/247_608.webm"},
        {"format_id": "398_608", "ext": "mp4", "vcodec": "av01.0.05M.08", "acodec": "none", "width": 608, "height": 1080, "tbr": 1050, "url": "https://cdn.example/398_608.mp4"},
        {"format_id": "136", "ext": "mp4", "vcodec": "avc1.4d401f", "acodec": "none", "width": 720, "height": 1280, "tbr": 1900, "url": "https://cdn.example/136.mp4"},
        {"format_id": "247", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 720, "height": 1280, "tbr": 1600, "url": "https://cdn.example/247.webm"},
        {"format_id": "398", "ext": "mp4", "vcodec": "av01.0.05M.08", "acodec": "none", "width": 720, "height": 1280, "tbr": 1400, "url": "https://cdn.example/398.mp4"},
        {"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none", "width": 1080, "height": 1920, "tbr": 3800, "url": "https://cdn.example/137.mp4"},
        {"format_id": "248", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 1080, "height": 1920, "tbr": 2900, "url": "https://cdn.example/248.webm"},
        {"format_id": "399", "ext": "mp4", "vcodec": "av01.0.08M.08", "acodec": "none", "width": 1080, "height": 1920, "tbr": 2500, "url": "https://cdn.example/399.mp4"},
        {"format_id": "271", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 1440, "height": 2560, "tbr": 6800, "url": "https://cdn.example/271.webm"},
        {"format_id": "400", "ext": "mp4", "vcodec": "av01.0.12M.08", "acodec": "none", "width": 1440, "height": 2560, "tbr": 5400, "url": "https://cdn.example/400.mp4"},
        {"format_id": "313", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 2160, "height": 3840, "tbr": 14500, "url": "https://cdn.example/313.webm"},
        {"format_id": "401", "ext": "mp4", "vcodec": "av01.0.12M.08", "acodec": "none", "width": 2160, "height": 3840, "tbr": 11500, "url": "https://cdn.example/401.mp4"},
    ]


def _canonical_landscape_formats():
    """Standard YouTube landscape ladder (256x144 .. 3840x2160)."""
    return [
        *_audio_formats(),
        {"format_id": "160", "ext": "mp4", "vcodec": "avc1.4d400c", "acodec": "none", "width": 256, "height": 144, "tbr": 110, "url": "https://cdn.example/160.mp4"},
        {"format_id": "133", "ext": "mp4", "vcodec": "avc1.4d4015", "acodec": "none", "width": 426, "height": 240, "tbr": 240, "url": "https://cdn.example/133.mp4"},
        {"format_id": "134", "ext": "mp4", "vcodec": "avc1.4d401e", "acodec": "none", "width": 640, "height": 360, "tbr": 520, "url": "https://cdn.example/134.mp4"},
        {"format_id": "18", "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "width": 640, "height": 360, "tbr": 580, "url": "https://cdn.example/18.mp4"},
        {"format_id": "135", "ext": "mp4", "vcodec": "avc1.4d401f", "acodec": "none", "width": 854, "height": 480, "tbr": 950, "url": "https://cdn.example/135.mp4"},
        {"format_id": "136", "ext": "mp4", "vcodec": "avc1.4d401f", "acodec": "none", "width": 1280, "height": 720, "tbr": 1900, "url": "https://cdn.example/136.mp4"},
        {"format_id": "247", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 1280, "height": 720, "tbr": 1600, "url": "https://cdn.example/247.webm"},
        {"format_id": "398", "ext": "mp4", "vcodec": "av01.0.05M.08", "acodec": "none", "width": 1280, "height": 720, "tbr": 1400, "url": "https://cdn.example/398.mp4"},
        {"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none", "width": 1920, "height": 1080, "tbr": 3800, "url": "https://cdn.example/137.mp4"},
        {"format_id": "248", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 1920, "height": 1080, "tbr": 2900, "url": "https://cdn.example/248.webm"},
        {"format_id": "399", "ext": "mp4", "vcodec": "av01.0.08M.08", "acodec": "none", "width": 1920, "height": 1080, "tbr": 2500, "url": "https://cdn.example/399.mp4"},
        {"format_id": "271", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 2560, "height": 1440, "tbr": 6800, "url": "https://cdn.example/271.webm"},
        {"format_id": "313", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 3840, "height": 2160, "tbr": 14500, "url": "https://cdn.example/313.webm"},
    ]


def _select_with_ytdlp(max_height, formats):
    spec = _build_video_format_spec(max_height)
    ydl = yt_dlp.YoutubeDL({
        "format": spec,
        "merge_output_format": "mp4",
        "quiet": True,
        "simulate": True,
    })
    info = {
        "id": "0tZcEWB1zPE",
        "title": "Fixture Video",
        "extractor": "youtube",
        "formats": [dict(f) for f in formats],
    }
    return ydl.process_ie_result(info, download=False)


# ── 1. Format selection (canonical vertical & landscape fixtures) ─────

def test_vertical_canonical_cap_1080_selects_1080x1920():
    selected = _select_with_ytdlp(1080, _canonical_vertical_formats())
    assert selected["format_id"] == "137+140"
    assert (selected["width"], selected["height"]) == (1080, 1920)
    assert selected["vcodec"].startswith("avc1")
    assert selected["acodec"].startswith("mp4a")


def test_vertical_canonical_cap_720_selects_720x1280():
    selected = _select_with_ytdlp(720, _canonical_vertical_formats())
    assert selected["format_id"] == "136+140"
    assert (selected["width"], selected["height"]) == (720, 1280)
    assert selected["vcodec"].startswith("avc1")
    assert selected["acodec"].startswith("mp4a")


def test_vertical_uncapped_prefers_best_h264_mp4():
    selected = _select_with_ytdlp(None, _canonical_vertical_formats())
    assert selected["format_id"] == "137+140"
    assert (selected["width"], selected["height"]) == (1080, 1920)


def test_landscape_canonical_unchanged():
    sel_1080 = _select_with_ytdlp(1080, _canonical_landscape_formats())
    assert sel_1080["format_id"] == "137+140"
    assert (sel_1080["width"], sel_1080["height"]) == (1920, 1080)

    sel_720 = _select_with_ytdlp(720, _canonical_landscape_formats())
    assert sel_720["format_id"] == "136+140"
    assert (sel_720["width"], sel_720["height"]) == (1280, 720)


def test_square_source_caps_cleanly():
    square_formats = [
        *_audio_formats(),
        {"format_id": "sq_720", "ext": "mp4", "vcodec": "avc1.4d401f", "acodec": "none", "width": 720, "height": 720, "tbr": 1500, "url": "https://cdn.example/sq720.mp4"},
        {"format_id": "sq_1080", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none", "width": 1080, "height": 1080, "tbr": 3000, "url": "https://cdn.example/sq1080.mp4"},
        {"format_id": "sq_1440", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none", "width": 1440, "height": 1440, "tbr": 5500, "url": "https://cdn.example/sq1440.mp4"},
    ]
    sel_1080 = _select_with_ytdlp(1080, square_formats)
    assert (sel_1080["width"], sel_1080["height"]) == (1080, 1080)

    sel_720 = _select_with_ytdlp(720, square_formats)
    assert (sel_720["width"], sel_720["height"]) == (720, 720)


def test_vertical_fallback_ladder_without_avc1_or_m4a():
    # No avc1 video-only and no m4a audio; only webm vp9 + opus + low pre-merged 360x640 mp4.
    webm_portrait = [
        {"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus", "abr": 160, "tbr": 160, "url": "https://cdn.example/251.webm"},
        {"format_id": "18", "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "width": 360, "height": 640, "tbr": 580, "url": "https://cdn.example/18.mp4"},
        {"format_id": "244", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 480, "height": 854, "tbr": 760, "url": "https://cdn.example/244.webm"},
        {"format_id": "247", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 720, "height": 1280, "tbr": 1600, "url": "https://cdn.example/247.webm"},
        {"format_id": "248", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 1080, "height": 1920, "tbr": 2900, "url": "https://cdn.example/248.webm"},
        {"format_id": "271", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 1440, "height": 2560, "tbr": 6800, "url": "https://cdn.example/271.webm"},
    ]
    sel_1080 = _select_with_ytdlp(1080, webm_portrait)
    assert sel_1080["format_id"] == "248+251"
    assert (sel_1080["width"], sel_1080["height"]) == (1080, 1920)

    sel_720 = _select_with_ytdlp(720, webm_portrait)
    assert sel_720["format_id"] == "247+251"
    assert (sel_720["width"], sel_720["height"]) == (720, 1280)


def test_vertical_premerged_only_ladder_selects_width_capped():
    # TikTok/IG/FB style: only pre-merged video+audio streams.
    premerged = [
        {"format_id": "p480", "ext": "mp4", "vcodec": "h264", "acodec": "aac", "width": 480, "height": 854, "tbr": 900, "url": "https://cdn.example/p480.mp4"},
        {"format_id": "p720", "ext": "mp4", "vcodec": "h264", "acodec": "aac", "width": 720, "height": 1280, "tbr": 1800, "url": "https://cdn.example/p720.mp4"},
        {"format_id": "p1080", "ext": "mp4", "vcodec": "h264", "acodec": "aac", "width": 1080, "height": 1920, "tbr": 3400, "url": "https://cdn.example/p1080.mp4"},
        {"format_id": "p1440", "ext": "mp4", "vcodec": "hevc", "acodec": "aac", "width": 1440, "height": 2560, "tbr": 5400, "url": "https://cdn.example/p1440.mp4"},
    ]
    sel_1080 = _select_with_ytdlp(1080, premerged)
    assert sel_1080["format_id"] == "p1080"
    assert (sel_1080["width"], sel_1080["height"]) == (1080, 1920)

    sel_720 = _select_with_ytdlp(720, premerged)
    assert sel_720["format_id"] == "p720"
    assert (sel_720["width"], sel_720["height"]) == (720, 1280)


def test_download_video_wires_orientation_aware_spec_end_to_end(monkeypatch, tmp_path):
    """Verify MediaDownloader.download_video passes an orientation-aware format
    spec through yt_dlp.YoutubeDL for both vertical and horizontal sources."""
    dest = tmp_path / "dl"
    dest.mkdir()
    real_ydl_cls = yt_dlp.YoutubeDL

    class _InspectingYDL:
        fixture_formats = []

        def __init__(self, opts):
            self.opts = opts
            self._real = real_ydl_cls({
                "format": opts["format"],
                "merge_output_format": opts.get("merge_output_format", "mp4"),
                "quiet": True,
                "simulate": True,
            })

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            info = {
                "id": "0tZcEWB1zPE",
                "title": "Short",
                "extractor": "youtube",
                "formats": [dict(f) for f in self.fixture_formats],
            }
            return self._real.process_ie_result(info, download=False)

        def prepare_filename(self, info):
            fp = dest / f"{info['id']}_{info['title']}.mp4"
            fp.write_bytes(b"mp4")
            return str(fp)

    monkeypatch.setattr(dl_mod, "unshorten_url", lambda u: u)
    monkeypatch.setattr(dl_mod.yt_dlp, "YoutubeDL", _InspectingYDL)
    dl = MediaDownloader(download_dir=str(tmp_path))

    _InspectingYDL.fixture_formats = _canonical_vertical_formats()
    _, vip_v = dl.download_video("https://www.youtube.com/watch?v=0tZcEWB1zPE", target_dir=str(dest), max_height=1080)
    assert (vip_v["width"], vip_v["height"]) == (1080, 1920)

    _, free_v = dl.download_video("https://www.youtube.com/watch?v=0tZcEWB1zPE", target_dir=str(dest), max_height=720)
    assert (free_v["width"], free_v["height"]) == (720, 1280)

    _InspectingYDL.fixture_formats = _canonical_landscape_formats()
    _, vip_h = dl.download_video("https://www.youtube.com/watch?v=0tZcEWB1zPE", target_dir=str(dest), max_height=1080)
    assert (vip_h["width"], vip_h["height"]) == (1920, 1080)

    _, free_h = dl.download_video("https://www.youtube.com/watch?v=0tZcEWB1zPE", target_dir=str(dest), max_height=720)
    assert (free_h["width"], free_h["height"]) == (1280, 720)


# ── 2. Local-file scaling (helper + real ffmpeg/ffprobe) ──────────────

@pytest.mark.parametrize(
    "width,height,cap,expected",
    [
        # Portrait: caps width, never upscales
        (1080, 1920, 1080, None),
        (1080, 1920, 720, "scale='min(720,iw)':-2"),
        (720, 1280, 720, None),
        (720, 1280, 1080, None),
        (480, 854, 720, None),
        (2160, 3840, 1080, "scale='min(1080,iw)':-2"),
        (2160, 3840, 720, "scale='min(720,iw)':-2"),
        # Landscape: caps height, never upscales
        (1920, 1080, 1080, None),
        (1920, 1080, 720, "scale=-2:'min(720,ih)'"),
        (1280, 720, 720, None),
        (1280, 720, 1080, None),
        (3840, 2160, 1080, "scale=-2:'min(1080,ih)'"),
        # Square & edge inputs
        (1080, 1080, 1080, None),
        (1080, 1080, 720, "scale=-2:'min(720,ih)'"),
        (1080, 1920, None, None),
        (None, 720, 720, None),
        (None, 1080, 720, "scale=-2:'min(720,ih)'"),
    ],
)
def test_local_video_scale_filter_matrix(width, height, cap, expected):
    assert _local_video_scale_filter(width, height, cap) == expected


def _make_sample_mp4(path: Path, width: int, height: int, *, vcodec: str = "libx264") -> Path:
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=blue:size={width}x{height}:rate=10:duration=0.5",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
        "-c:v", vcodec, "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not available")
def test_process_local_video_portrait_and_landscape_real_ffmpeg(tmp_path):
    dl = MediaDownloader(download_dir=str(tmp_path / "dl"))

    # Portrait 1080x1920:
    # - cap 1080 -> untouched 1080x1920 (no scale!)
    # - cap 720  -> downscaled to 720x1280 (NOT 404x720!)
    p1080 = _make_sample_mp4(tmp_path / "portrait_1080.mp4", 1080, 1920)
    out_p_vip, _ = dl.process_local_video(str(p1080), target_dir=str(tmp_path / "out_p_vip"), max_height=1080)
    meta_p_vip = probe_local_media(out_p_vip)
    assert (meta_p_vip["width"], meta_p_vip["height"]) == (1080, 1920)

    out_p_free, _ = dl.process_local_video(str(p1080), target_dir=str(tmp_path / "out_p_free"), max_height=720)
    meta_p_free = probe_local_media(out_p_free)
    assert (meta_p_free["width"], meta_p_free["height"]) == (720, 1280)

    # Portrait 720x1280 under cap 1080: never upscales
    p720 = _make_sample_mp4(tmp_path / "portrait_720.mp4", 720, 1280)
    out_p720, _ = dl.process_local_video(str(p720), target_dir=str(tmp_path / "out_p720"), max_height=1080)
    meta_p720 = probe_local_media(out_p720)
    assert (meta_p720["width"], meta_p720["height"]) == (720, 1280)

    # Landscape 1920x1080:
    # - cap 1080 -> untouched 1920x1080
    # - cap 720  -> downscaled to 1280x720
    l1080 = _make_sample_mp4(tmp_path / "landscape_1080.mp4", 1920, 1080)
    out_l_vip, _ = dl.process_local_video(str(l1080), target_dir=str(tmp_path / "out_l_vip"), max_height=1080)
    meta_l_vip = probe_local_media(out_l_vip)
    assert (meta_l_vip["width"], meta_l_vip["height"]) == (1920, 1080)

    out_l_free, _ = dl.process_local_video(str(l1080), target_dir=str(tmp_path / "out_l_free"), max_height=720)
    meta_l_free = probe_local_media(out_l_free)
    assert (meta_l_free["width"], meta_l_free["height"]) == (1280, 720)

    # Landscape 1280x720 under cap 1080: never upscales
    l720 = _make_sample_mp4(tmp_path / "landscape_720.mp4", 1280, 720)
    out_l720, _ = dl.process_local_video(str(l720), target_dir=str(tmp_path / "out_l720"), max_height=1080)
    meta_l720 = probe_local_media(out_l720)
    assert (meta_l720["width"], meta_l720["height"]) == (1280, 720)


@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not available")
def test_process_local_video_rotated_phone_portrait(tmp_path):
    """A 1920x1080 coded stream with display-matrix rotate=90 is 1080x1920 portrait."""
    coded = tmp_path / "phone_coded.mp4"
    raw = tmp_path / "phone_rot.mp4"
    subprocess.run(
        [
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=green:size=1920x1080:rate=10:duration=0.5",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(coded),
        ],
        check=True, capture_output=True,
    )
    # Apply MP4 Display Matrix rotation via stream copy (newer ffmpeg ignores
    # `-metadata:s:v:0 rotate=90` on encode).
    subprocess.run(
        [
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-display_rotation:v:0", "90", "-i", str(coded),
            "-c", "copy", str(raw),
        ],
        check=True, capture_output=True,
    )
    probed = probe_local_media(str(raw))
    assert (probed["width"], probed["height"]) == (1080, 1920)

    dl = MediaDownloader(download_dir=str(tmp_path / "dl"))
    out_vip, _ = dl.process_local_video(str(raw), target_dir=str(tmp_path / "vip"), max_height=1080)
    meta_vip = probe_local_media(out_vip)
    assert (meta_vip["width"], meta_vip["height"]) == (1080, 1920)

    out_free, _ = dl.process_local_video(str(raw), target_dir=str(tmp_path / "free"), max_height=720)
    meta_free = probe_local_media(out_free)
    assert (meta_free["width"], meta_free["height"]) == (720, 1280)


def test_probe_local_media_legacy_rotate_tag_swaps_axes(monkeypatch, tmp_path):
    """Older MOV/MP4 files store rotation in stream tags.rotate instead of side_data_list."""
    import json

    fake_vid = tmp_path / "legacy.mp4"
    fake_vid.write_bytes(b"\x00" * 16)
    payload = {
        "format": {"duration": "5.0", "size": "16"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "tags": {"rotate": "270"},
            }
        ],
    }
    monkeypatch.setattr(
        dl_mod.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(payload)),
    )
    probed = probe_local_media(str(fake_vid))
    assert (probed["width"], probed["height"]) == (1080, 1920)


def test_generate_recap_and_preview_forward_tier_cap(monkeypatch, tmp_path):
    """Tier cap values (VIP 1080 / FREE 720) are forwarded unchanged to download_video."""
    dl = MediaDownloader(download_dir=str(tmp_path / "dl"))
    src = tmp_path / "src.mp4"
    src.write_bytes(b"")
    seen_caps: list[tuple[str, int | None]] = []

    def _fake_download_video(url, target_dir=None, max_height=None, **kwargs):
        seen_caps.append((url, max_height))
        return str(src), {"id": "short1"}

    monkeypatch.setattr(dl, "download_video", _fake_download_video)
    monkeypatch.setattr(dl_mod, "probe_local_media", lambda p: {"duration": 10.0})
    monkeypatch.setattr(dl_mod, "run_recap_pipeline", lambda **kw: {"stage_timings": {}})
    monkeypatch.setattr(dl_mod, "generate_recap_preview", lambda **kw: "preview.jpg")

    url = "https://www.youtube.com/shorts/abc123"
    dl.generate_recap_video(url, target_dir=str(tmp_path / "r_vip"), is_vip=True)
    dl.generate_recap_video(url, target_dir=str(tmp_path / "r_free"), is_vip=False)
    dl.generate_recap_preview(url, target_dir=str(tmp_path / "p_vip"), at_seconds=1.0, is_vip=True)
    dl.generate_recap_preview(url, target_dir=str(tmp_path / "p_free"), at_seconds=1.0, is_vip=False)

    assert seen_caps == [
        (url, 1080),
        (url, 720),
        (url, 1080),
        (url, 720),
    ]
