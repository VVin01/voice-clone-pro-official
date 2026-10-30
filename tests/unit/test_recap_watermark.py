"""Tests for the free-tier brand watermark v3 (2026-10-30).

v3 = full-frame diagonal tiled pattern of the bot username
"@voiceclonepro_bot" ALONE — no corner lockup. v1 was corner-only and
could simply be cropped away; v2 paired the pattern with a corner
lockup, which the owner rejected as a cheap-looking sticker that
collided with a pattern tile. The pattern is composited after subtitles
in the overlay pass (which itself runs after the mirror/zoom part
renders, so the mark is never mirrored). Paid output (no watermark
flag) stays clean.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.recap_engine.core import (
    BRAND_PATTERN_OPACITY,
    BRAND_PATTERN_TEXT,
    render_brand_pattern_png,
)

FFMPEG = shutil.which("ffmpeg")


def test_pattern_text_is_bot_username():
    assert BRAND_PATTERN_TEXT == "@voiceclonepro_bot"


def _region_has_mark(alpha, box, threshold=30):
    region = alpha.crop(box)
    data = list(region.getdata())
    return sum(1 for v in data if v > 25) > threshold


def test_pattern_png_covers_centre_and_edges(tmp_path):
    from PIL import Image

    # Production-representative size (720p): the grid geometry is tuned
    # for real output frames, and coverage was frame-verified at this size.
    out = render_brand_pattern_png(tmp_path / "pat.png", frame_width=1280, frame_height=720)
    img = Image.open(out)
    assert img.size == (1280, 720)
    alpha = img.getchannel("A")
    # Mostly transparent (it is a texture, not a panel) ...
    total = 1280 * 720
    visible = sum(1 for v in alpha.getdata() if v > 25)
    assert 0.005 < visible / total < 0.30
    # ... but the mark reaches the centre AND every corner/edge zone, so
    # no crop can remove it.
    zones = {
        "centre": (512, 288, 768, 432),
        "top-left": (0, 0, 256, 144),
        "top-right": (1024, 0, 1280, 144),
        "bottom-left": (0, 576, 256, 720),
        "bottom-right": (1024, 576, 1280, 720),
    }
    for name, box in zones.items():
        assert _region_has_mark(alpha, box, threshold=120), f"no pattern in {name}"
    # Opacity is honoured: a stronger render has strictly more alpha mass.
    strong = Image.open(
        render_brand_pattern_png(tmp_path / "pat2.png", frame_width=1280,
                                 frame_height=720, opacity=0.34)
    ).getchannel("A")
    assert sum(strong.getdata()) > sum(alpha.getdata())
    assert 0.08 <= BRAND_PATTERN_OPACITY <= 0.35


def _make_clip(path, w=640, h=360):
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"testsrc2=size={w}x{h}:rate=30:duration=2", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True,
    )


def _frame_region(video, tmp_path, box, name):
    """Mean brightness of a frame region."""
    frame = tmp_path / name
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", "1.0",
         "-i", str(video), "-frames:v", "1", str(frame)],
        check=True, capture_output=True,
    )
    from PIL import Image

    img = Image.open(frame).convert("L").crop(box)
    data = list(img.getdata())
    return sum(data) / len(data)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_free_gets_pattern_paid_stays_clean(tmp_path):
    from app.services.recap_engine.core import apply_production_overlays

    src = tmp_path / "src.mp4"
    _make_clip(src)
    layout = {"pw": 640, "ph": 360, "ox": 0, "oy": 0, "sw": 640, "sh": 360}

    free_out = tmp_path / "free.mp4"
    apply_production_overlays(
        FFMPEG, src, free_out,
        {"extra_text": "branding", "watermark": True},
        "libx264", tmp_path / "wfree", frame_size=(640, 360), layout=layout,
    )
    # No overlays at all: the pass is a no-op and returns the input itself —
    # which is exactly why paid output can never gain a watermark.
    paid_out = apply_production_overlays(
        FFMPEG, src, tmp_path / "paid.mp4", {},
        "libx264", tmp_path / "wpaid", frame_size=(640, 360), layout=layout,
    )
    assert Path(paid_out) == src

    # The pattern changes pixels across the whole frame; compare the
    # centre region (a corner-only mark could never reach it) by mean
    # absolute pixel difference — the 0.19-opacity mark barely moves a
    # busy frame's mean brightness, but its text + shadow twin must
    # still register as a real per-pixel difference.
    def centre_mean_abs_diff(a, b):
        fa, fb = tmp_path / "da.png", tmp_path / "db.png"
        for video, out in ((a, fa), (b, fb)):
            subprocess.run(
                [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", "1.0",
                 "-i", str(video), "-frames:v", "1", str(out)],
                check=True, capture_output=True,
            )
        from PIL import Image, ImageChops

        ia = Image.open(fa).convert("L").crop((160, 90, 480, 270))
        ib = Image.open(fb).convert("L").crop((160, 90, 480, 270))
        diff = list(ImageChops.difference(ia, ib).getdata())
        return sum(diff) / len(diff)

    mad = centre_mean_abs_diff(free_out, src)
    assert mad > 1.0, mad


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_pattern_survives_centre_crop_and_paid_is_clean(tmp_path):
    """Crop-attack at render level: the centre of a free frame must still
    carry the mark, and the watermark flag alone must trigger the overlay
    pass."""
    from app.services.recap_engine.core import apply_production_overlays

    # Solid dark source at production size: any white pattern text in the
    # centre is unmistakable, not test-pattern luck.
    src = tmp_path / "src.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=0x202030:size=1280x720:rate=30:duration=2", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )
    layout = {"pw": 1280, "ph": 720, "ox": 0, "oy": 0, "sw": 1280, "sh": 720}
    centre = (384, 216, 896, 504)  # centre region — pattern must reach here

    free_out = tmp_path / "free.mp4"
    result = apply_production_overlays(
        FFMPEG, src, free_out,
        {"watermark": True},  # flag alone must trigger the pass (guard fix)
        "libx264", tmp_path / "wf", frame_size=(1280, 720), layout=layout,
    )
    assert Path(result).is_file() and Path(result) != src

    def bright_centre(video, name):
        frame = tmp_path / name
        subprocess.run(
            [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", "1.0",
             "-i", str(video), "-frames:v", "1", str(frame)],
            check=True, capture_output=True,
        )
        from PIL import Image

        img = Image.open(frame).convert("L").crop(centre)
        return sum(1 for v in img.getdata() if v > 70)

    # Pattern text pixels are far brighter than the solid 0x202030 field;
    # the plain source centre has essentially none.
    assert bright_centre(src, "sc.png") < 50
    assert bright_centre(free_out, "fc.png") > 800
