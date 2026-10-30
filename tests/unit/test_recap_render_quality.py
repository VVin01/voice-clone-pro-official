"""Tests for render quality presets (production-grade Feature 3)."""
import shutil
import subprocess

import pytest

from app.services.recap_engine.core import (
    RENDER_QUALITIES,
    _encoder_args,
    encoder_args_for_quality,
    normalize_render_quality,
)

HISTORICAL_X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-threads", "0"]
HISTORICAL_NVENC = ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "20", "-b:v", "0"]


def test_standard_is_byte_identical_to_historical_default():
    assert _encoder_args("libx264") == HISTORICAL_X264
    assert _encoder_args("h264_nvenc") == HISTORICAL_NVENC
    assert encoder_args_for_quality("libx264", "standard") == HISTORICAL_X264
    assert encoder_args_for_quality("h264_nvenc", "standard") == HISTORICAL_NVENC
    # Default argument is standard.
    assert encoder_args_for_quality("libx264") == HISTORICAL_X264


def test_high_and_max_lower_the_quantizers():
    def arg(args, flag):
        return args[args.index(flag) + 1]

    assert int(arg(encoder_args_for_quality("libx264", "high"), "-crf")) < 22
    assert int(arg(encoder_args_for_quality("libx264", "max"), "-crf")) < int(
        arg(encoder_args_for_quality("libx264", "high"), "-crf"))
    assert int(arg(encoder_args_for_quality("h264_nvenc", "high"), "-cq")) < 20
    assert int(arg(encoder_args_for_quality("h264_nvenc", "max"), "-cq")) < int(
        arg(encoder_args_for_quality("h264_nvenc", "high"), "-cq"))
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
