"""Tests for render quality presets (production-grade Feature 3)."""
import shutil
import subprocess

import pytest

from app.services.recap_engine.core import (
    RENDER_QUALITIES,
    encoder_args_for_quality,
    normalize_render_quality,
)

HISTORICAL_X264 = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
    "-maxrate", "7200k", "-bufsize", "14400k", "-threads", "0",
]
HISTORICAL_NVENC = [
    "-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0",
    "-maxrate", "7200k", "-bufsize", "14400k",
]


def test_standard_is_byte_identical_to_historical_default():
    assert encoder_args_for_quality("libx264", "standard") == HISTORICAL_X264
    assert encoder_args_for_quality("h264_nvenc", "standard") == HISTORICAL_NVENC
    # Default argument is standard.
    assert encoder_args_for_quality("libx264") == HISTORICAL_X264


def test_high_and_max_lower_the_quantizers():
    def arg(args, flag):
        return args[args.index(flag) + 1]

    def kbps(s):
        return int(s[:-1]) if s.endswith("k") else int(s)

    assert int(arg(encoder_args_for_quality("libx264", "high"), "-crf")) < 23
    assert int(arg(encoder_args_for_quality("libx264", "max"), "-crf")) < int(
        arg(encoder_args_for_quality("libx264", "high"), "-crf"))
    assert int(arg(encoder_args_for_quality("h264_nvenc", "high"), "-cq")) < 23
    assert int(arg(encoder_args_for_quality("h264_nvenc", "max"), "-cq")) < int(
        arg(encoder_args_for_quality("h264_nvenc", "high"), "-cq"))
    for enc in ("libx264", "h264_nvenc"):
        std_r = kbps(arg(encoder_args_for_quality(enc, "standard"), "-maxrate"))
        hi_r = kbps(arg(encoder_args_for_quality(enc, "high"), "-maxrate"))
        max_r = kbps(arg(encoder_args_for_quality(enc, "max"), "-maxrate"))
        assert 6000 <= std_r <= 8000
        assert 10000 <= hi_r <= 12000
        assert std_r < hi_r < max_r
    assert set(RENDER_QUALITIES) == {"standard", "high", "max"}


def test_unknown_quality_falls_back_to_standard():
    assert normalize_render_quality("ultra") == "standard"
    assert normalize_render_quality("") == "standard"
    assert normalize_render_quality(None) == "standard"
    assert normalize_render_quality(" HIGH ") == "high"
    assert encoder_args_for_quality("libx264", "bogus") == HISTORICAL_X264
    # Unknown encoder falls back to the libx264 row of the same quality.
    assert encoder_args_for_quality("vp9", "high") == encoder_args_for_quality("libx264", "high")


def test_args_are_copies_not_shared_lists():
    a = encoder_args_for_quality("libx264", "high")
    a.append("junk")
    assert "junk" not in encoder_args_for_quality("libx264", "high")


def test_sync_engine_reads_quality_from_overlays():
    from app.services.recap_engine.core import EdgeSmartSync

    eng = EdgeSmartSync(
        job_dir="/tmp", voice="my-MM-ThihaNeural",
        overlays={"render_quality": "max"},
    )
    assert eng.render_quality == "max"
    eng2 = EdgeSmartSync(job_dir="/tmp", voice="my-MM-ThihaNeural", overlays={})
    assert eng2.render_quality == "standard"
    eng3 = EdgeSmartSync(
        job_dir="/tmp", voice="my-MM-ThihaNeural",
        overlays={"render_quality": "bogus"},
    )
    assert eng3.render_quality == "standard"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_render_video_part_high_quality_produces_bigger_file(tmp_path):
    """Same clip at CRF 22 vs CRF 15: the max-quality encode must not be
    smaller (more bits kept) and must be a valid MP4."""
    from app.services.recap_engine.core import render_video_part

    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=duration=3:size=320x180:rate=30",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(src)],
        check=True,
    )
    seg = {"video_start": 0.0, "video_end": 3.0}
    out_std = tmp_path / "std.mp4"
    out_max = tmp_path / "max.mp4"
    render_video_part("ffmpeg", src, out_std, seg, 3.0, "libx264",
                      mirror_video=False, zoom_factor=1.0, render_quality="standard")
    render_video_part("ffmpeg", src, out_max, seg, 3.0, "libx264",
                      mirror_video=False, zoom_factor=1.0, render_quality="max")
    assert out_std.stat().st_size > 5000
    assert out_max.stat().st_size >= out_std.stat().st_size


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_a2_standard_vbv_caps_bitrate_and_preserves_subtitle_crispness(tmp_path):
    """A2: Standard recap + strong protection stays inside ~6-8 Mbps target on 1080p
    grain while keeping burned-in subtitle edges crisp (SSIM >= 0.90)."""
    import re
    from PIL import Image, ImageDraw
    from app.services.recap_engine.core import apply_protection_strong

    sub_png = tmp_path / "sub.png"
    im = Image.new("RGBA", (640, 96), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((8, 6, 632, 90), radius=14, fill=(10, 12, 18, 230), outline=(255, 220, 80, 255), width=3)
    for x in range(40, 600, 28):
        d.rectangle((x, 26, x + 16, 70), fill=(255, 255, 255, 255))
    im.save(sub_png)

    src = tmp_path / "src_hd.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30,noise=alls=8:allf=t",
        "-t", "6", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-threads", "0", "-pix_fmt", "yuv420p", str(src),
    ], check=True)

    ref = tmp_path / "ref.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-i", str(sub_png),
        "-filter_complex", "[0:v][1:v]overlay=x='(W-w)/2':y='H*0.78':eof_action=repeat,format=yuv420p[vout]",
        "-map", "[vout]", "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0", "-threads", "0", str(ref),
    ], check=True)

    out_old = tmp_path / "old.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-i", str(sub_png),
        "-filter_complex", "[0:v][1:v]overlay=x='(W-w)/2':y='H*0.78':eof_action=repeat,format=yuv420p[vout]",
        "-map", "[vout]", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-threads", "0", str(out_old),
    ], check=True)

    out_std = tmp_path / "std.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-i", str(sub_png),
        "-filter_complex", "[0:v][1:v]overlay=x='(W-w)/2':y='H*0.78':eof_action=repeat,format=yuv420p[vout]",
        "-map", "[vout]", "-an", *encoder_args_for_quality("libx264", "standard"), str(out_std),
    ], check=True)

    prot = apply_protection_strong("ffmpeg", out_std, render_quality="standard")
    assert prot.is_file()
    old_mbps = (out_old.stat().st_size * 8) / (6.0 * 1_000_000)
    std_mbps = (out_std.stat().st_size * 8) / (6.0 * 1_000_000)
    prot_mbps = (prot.stat().st_size * 8) / (6.0 * 1_000_000)
    assert std_mbps < old_mbps * 0.70
    assert std_mbps <= 8.5
    assert prot_mbps <= 8.8

    sp = subprocess.run([
        "ffmpeg", "-hide_banner", "-i", str(out_std), "-i", str(ref),
        "-filter_complex", "[0:v]crop=640:96:220:1497[a];[1:v]crop=640:96:220:1497[b];[a][b]ssim",
        "-t", "2", "-f", "null", "-",
    ], capture_output=True, text=True, check=True)
    m = re.search(r"All:([0-9.]+)", sp.stderr)
    assert m and float(m.group(1)) >= 0.90
