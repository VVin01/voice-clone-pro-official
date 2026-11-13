"""Batch Q (2026-11-04): PNG-overlay subtitle burn + size bump.

The shaping diagnosis (~/workspace/subtitle-compare/shaping) proved
the subtitle TEXT was always codepoint-correct; the "misspelled"
look was legibility — small Regular captions rasterised softly by
libass. Batch Q therefore:

* replaces ALL THREE libass burns (ASS path, SRT force_style
  fallback, engine preview) with per-cue Pillow+raqm PNG overlays
  (png_subtitles.py) — fonts loaded by path from the bundled static
  builds, RAQM asserted loudly, presets mapped to fill/stroke/band;
* raises the shared height-fraction table one notch (16pt default
  3.2% → 4.0% of canvas height) in core.py AND the app.js mirror;
* leaves the downloadable .srt side-file and every blur/plate/
  watermark invariant from Batches J/P untouched.

Pins below cover the renderer (production, order, cache, preset
mapping, two-line stacking), text byte-identity with the old ASS
burn, the new size table + preview parity, the RAQM guard, the
font-file rule (static Bold, never the variable font), the
file-fallback burns, and an end-to-end overlay render.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = ROOT / "app" / "services" / "recap_engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import core  # noqa: E402
import png_subtitles as pq  # noqa: E402
from core import (  # noqa: E402
    SUBTITLE_STYLE_PRESETS,
    split_burmese_subtitle_cues,
    subtitle_font_file,
    subtitle_font_px,
    write_synced_ass_file,
)

APP_JS = (ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
CORE_PY = (ENGINE_DIR / "core.py").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")

FFMPEG = shutil.which("ffmpeg")

PROBE = "မြို့ကြီးဆီကို ထွက်ခွာပြီး သင်္ကြန်ပွဲတော်မှာ ကျောင်းသားတွေ လွှမ်းမိုးနေတဲ့ ပြဿနာကို ဆုံးဖြတ်ခဲ့ကြပါတယ်"
GOLD = SUBTITLE_STYLE_PRESETS["yellow"]


def _renderer(tmp_path: Path) -> pq.PngSubtitleRenderer:
    return pq.PngSubtitleRenderer(tmp_path / "cache")


def _visuals(preset=GOLD, ch=720, font_px=29):
    return pq.preset_visuals(preset, ch, font_px)


# ── 1. RAQM guard ──────────────────────────────────────────────────

def test_raqm_assertion_present_and_passes():
    src = (ENGINE_DIR / "png_subtitles.py").read_text(encoding="utf-8")
    assert "features.check(\"raqm\")" in src or 'features.check(\'raqm\')' in src
    assert "Layout.RAQM" in src
    pq.assert_raqm()  # must not raise in this environment


def test_no_basic_layout_fallback_anywhere():
    src = (ENGINE_DIR / "png_subtitles.py").read_text(encoding="utf-8")
    assert "Layout.BASIC" not in src


# ── 2. Font file rule: static Bold by path, variable font banned ──

def test_font_file_is_bundled_static_bold():
    p = subtitle_font_file("noto_sans")
    assert p.name == "NotoSansMyanmar-Bold.ttf"
    assert p.is_file()
    for key in ("padauk", "noto_sans", "pyidaungsu", "noto_serif"):
        p = subtitle_font_file(key)
        assert "Bold" in p.name
        # The variable font that merged stacks (2026-11-01) can
        # never be selected: it is in no option's file list.
        assert p.name != "NotoSansMyanmar.ttf"


# ── 3. Renderer: production, order, cache ──────────────────────────

def test_render_produces_png_per_cue_in_order(tmp_path):
    r = _renderer(tmp_path)
    font = subtitle_font_file("default")
    v = _visuals()
    out1 = r.render_text("မြို့ကြီးဆီကို ထွက်ခွာပြီး", 1, 29, font, v)
    out2 = r.render_text("ပြဿနာကို ဆုံးဖြတ်ခဲ့ကြပါတယ်", 1, 29, font, v)
    assert out1 is not None and out2 is not None
    p1, w1, h1 = out1
    p2, w2, h2 = out2
    assert p1.is_file() and p2.is_file() and p1 != p2
    assert w1 > 0 and h1 > 0 and w2 > 0 and h2 > 0
    from PIL import Image

    with Image.open(p1) as im:
        assert im.mode == "RGBA"
        # Transparent corners: it is an overlay, not a full frame.
        assert im.getpixel((0, 0))[3] == 0


def test_render_cache_reuse_and_style_separation(tmp_path):
    r = _renderer(tmp_path)
    font = subtitle_font_file("default")
    a = r.render_text("မင်္ဂလာပါ", 1, 29, font, _visuals())
    mtime = a[0].stat().st_mtime_ns
    b = r.render_text("မင်္ဂလာပါ", 1, 29, font, _visuals())
    assert a[0] == b[0]
    assert b[0].stat().st_mtime_ns == mtime  # cache hit: not rewritten
    c = r.render_text(
        "မင်္ဂလာပါ", 1, 29, font,
        _visuals(SUBTITLE_STYLE_PRESETS["white"]),
    )
    assert c[0] != a[0]  # different style → different cache entry


def test_render_empty_cue_is_none(tmp_path):
    r = _renderer(tmp_path)
    assert r.render_text("   ", 1, 29, subtitle_font_file("default"), _visuals()) is None


# ── 4. Preset → fill/stroke/band mapping ───────────────────────────

def test_preset_visuals_gold_fill_and_stroke():
    v = _visuals()
    assert v["fill"] == (255, 255, 0, 255)  # Classic Gold yellow
    assert v["stroke_colour"] == (0, 0, 0, 255)
    assert v["band"] is None  # border_style 1 → no band


def test_preset_stroke_uses_r40_cap_and_straddle_calibration():
    for key, preset in SUBTITLE_STYLE_PRESETS.items():
        for font_px in (18, 29, 61, 96):
            outl = pq.subtitle_outline_px(preset, 1920, font_px)
            assert outl <= max(1.5, 0.08 * font_px) + 1e-9, key
            v = pq.preset_visuals(preset, 1920, font_px)
            assert v["stroke_px"] == max(1, round(outl * 0.75)), key


def test_band_presets_get_rounded_rect_backing():
    for key in ("boxed", "band"):
        v = _visuals(SUBTITLE_STYLE_PRESETS[key])
        assert v["band"] is not None, key
        assert v["band"][:3] == (0, 0, 0)
        assert 0 < v["band"][3] < 255  # translucent, from back_col alpha
    for key in ("yellow", "white", "neon", "heavy", "soft"):
        assert _visuals(SUBTITLE_STYLE_PRESETS[key])["band"] is None, key


def test_all_seven_presets_survive():
    # Batch DD: the original seven survive, plus three more
    # (rose / orange / mint) for the rebuilt Step-2 style picker.
    assert set(SUBTITLE_STYLE_PRESETS) == {
        "yellow", "white", "boxed", "neon", "band", "heavy", "soft",
        "rose", "orange", "mint",
    }
    heavy = _visuals(SUBTITLE_STYLE_PRESETS["heavy"])
    gold = _visuals(GOLD)
    assert heavy["stroke_px"] > gold["stroke_px"]


# ── 5. Two-line stacking ───────────────────────────────────────────

def test_two_line_cue_stacks_taller(tmp_path):
    r = _renderer(tmp_path)
    font = subtitle_font_file("default")
    v = _visuals()
    one = r.render_text("မြို့ကြီးဆီကို ထွက်ခွာပြီး", 1, 29, font, v)
    two = r.render_text(
        "မြို့ကြီးဆီကို ထွက်ခွာပြီး\nပြဿနာကို ဆုံးဖြတ်ခဲ့ကြပါတယ်", 2, 29, font, v
    )
    assert two[2] > one[2]  # stacked block is taller
    assert two[0] != one[0]


# ── 6. Text byte-identity with the old ASS burn ────────────────────

def _ass_dialogue_texts(path: Path) -> list[str]:
    texts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("Dialogue:"):
            texts.append(line.split(",", 9)[9].replace("\\N", "\n"))
    return texts


def test_png_text_byte_identical_to_ass_burn(tmp_path):
    cues = split_burmese_subtitle_cues(
        0.0, 12.0, PROBE, max_cue_chars=52, lines_per_cue=1
    )
    assert len(cues) >= 2
    ass = write_synced_ass_file(cues, tmp_path / "probe.ass", 1280, 720)
    ass_texts = _ass_dialogue_texts(ass)
    png_texts = [" ".join(pq.normalize_cue_lines(t, 1)) for _a, _b, t in cues]
    assert png_texts == ass_texts
    # The cluster-dense words survive whole in both.
    joined = "".join(png_texts)
    for word in ("မြို့ကြီး", "သင်္ကြန်", "လွှမ်းမိုး", "ပြဿနာ", "ဆုံးဖြတ်"):
        assert word in joined


def test_normalize_cue_lines_rules():
    assert pq.normalize_cue_lines("a  b\nc", 1) == ["a b c"]
    assert pq.normalize_cue_lines("l1\nl2", 2) == ["l1", "l2"]
    assert pq.normalize_cue_lines("  ", 1) == []


# ── 7. New size table + preview mirror parity ──────────────────────

@pytest.mark.parametrize(
    "pt,frac", [(13, 0.032), (16, 0.040), (19, 0.048), (22, 0.056)]
)
def test_new_font_fraction_table(pt, frac):
    assert core._subtitle_font_fraction(pt) == pytest.approx(frac)


def test_default_size_bump_exact_px():
    # The approved bump: 16pt default 3.2% → 4.0% of canvas height.
    assert subtitle_font_px(1280, 720, 16, 1, None) == 29   # was 23
    assert subtitle_font_px(1080, 1920, 16, 1, None) == 77  # was 61
    assert subtitle_font_px(1920, 1080, 16, 1, None) == 43  # was 35
    assert subtitle_font_px(1080, 1920, 16, 2, None) == 96  # two-line ×1.25
    sizes = {pt: subtitle_font_px(1080, 1920, pt, 1, None) for pt in (13, 16, 19, 22)}
    assert sizes == {13: 61, 16: 77, 19: 92, 22: 108}
    assert len(set(sizes.values())) == 4


def test_preview_mirror_matches_new_table():
    assert "[[13, 3.2], [16, 4.0], [19, 4.8], [22, 5.6]]" in APP_JS
    for _pt, frac in core.SUBTITLE_FONT_HEIGHT_FRACTIONS:
        assert frac in (0.032, 0.040, 0.048, 0.056)


# ── 8. The libass burn is gone from both graph builders ────────────

def test_no_libass_burn_left_in_overlay_paths():
    apply_body = CORE_PY.split("def apply_production_overlays(", 1)[1]
    apply_body = apply_body.split("def extract_preview_with_overlays(", 1)[0]
    assert "force_style" not in apply_body
    assert "subtitles='" not in apply_body
    assert "_png_subs.PngSubtitleRenderer" in apply_body
    assert "subtitle_cues" in CORE_PY  # pipeline hands cues to the burn
    preview_body = CORE_PY.split("def extract_preview_with_overlays(", 1)[1]
    preview_body = preview_body.split("class PipelinePaths", 1)[0]
    assert "subtitles='" not in preview_body
    assert "_png_subs.PngSubtitleRenderer" in preview_body


# ── 9. File-fallback cue sources ───────────────────────────────────

def test_parse_srt_cues_roundtrip(tmp_path):
    srt = tmp_path / "x.srt"
    srt.write_text(
        "1\n00:00:00,500 --> 00:00:02,500\nမြို့ကြီးဆီကို\n\n"
        "2\n00:00:03,000 --> 00:00:05,250\nပြဿနာ\n",
        encoding="utf-8",
    )
    cues = pq.parse_srt_cues(srt)
    assert cues == [(0.5, 2.5, "မြို့ကြီးဆီကို"), (3.0, 5.25, "ပြဿနာ")]


def test_parse_ass_source_roundtrip(tmp_path):
    ass = write_synced_ass_file(
        [(0.5, 2.5, "မြို့ကြီးဆီကို ထွက်ခွာပြီး")], tmp_path / "x.ass",
        1280, 720, raw_font_size=16, sub_y=0.85,
    )
    parsed = pq.parse_ass_source(ass)
    assert parsed is not None
    assert parsed["cues"] == [(0.5, 2.5, "မြို့ကြီးဆီကို ထွက်ခွာပြီး")]
    assert parsed["font_px"] == subtitle_font_px(1280, 720, 16, 1, None)
    assert parsed["font_family"] == "Padauk"  # un-chosen default family
    assert parsed["visuals"]["fill"] == (255, 255, 0, 255)


# ── 10. End-to-end overlay renders (ffmpeg) ────────────────────────

def _make_src(tmp_path: Path) -> Path:
    src = tmp_path / "src.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=0x202030:size=320x240:rate=30:duration=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )
    return src


def _frame_band_mean(path: Path, tmp_path: Path, tsec: float) -> float:
    frame = tmp_path / f"frame_{tsec}.png"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", str(tsec),
         "-i", str(path), "-frames:v", "1", str(frame)],
        check=True, capture_output=True,
    )
    from PIL import Image

    band = Image.open(frame).convert("L").crop((20, 150, 300, 235))
    data = list(band.getdata())
    return sum(data) / len(data)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_end_to_end_png_burn_with_cues_and_blur(tmp_path):
    src = _make_src(tmp_path)
    out = tmp_path / "out.mp4"
    core.apply_production_overlays(
        FFMPEG, src, out,
        {
            "subtitle_cues": [[0.5, 2.5, "မြို့ကြီးဆီကို ထွက်ခွာပြီး သင်္ကြန်ပွဲတော်"]],
            "subtitles_font_size": 16, "sub_style": "yellow",
            "sub_lines": 1, "sub_y": 0.85,
            "blur_boxes": [{"x": 0.05, "y": 0.85, "w": 0.90, "h": 0.16}],
            "blur_strength": 18,
        },
        "libx264", tmp_path / "w", frame_size=(320, 240),
        layout={"pw": 320, "ph": 240, "ox": 0, "oy": 0}, duration=4,
    )
    assert out.is_file() and out.stat().st_size > 5000
    # Cue visible at t=1.5, gone at t=3.5: the band lights up only
    # while the cue is on screen.
    assert _frame_band_mean(out, tmp_path, 1.5) > _frame_band_mean(out, tmp_path, 3.5) + 2.0
    # The PNG cache was populated beside the work dir.
    assert list((tmp_path / "w" / "subtitle_png_cache").glob("sub_*.png"))


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_end_to_end_srt_file_fallback_burns_png(tmp_path):
    src = _make_src(tmp_path)
    srt = tmp_path / "x.srt"
    srt.write_text(
        "1\n00:00:00,500 --> 00:00:02,500\nမြို့ကြီးဆီကို ထွက်ခွာပြီး\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.mp4"
    core.apply_production_overlays(
        FFMPEG, src, out,
        {"subtitles_file": str(srt), "sub_y": 0.85,
         "subtitles_font_size": 16, "sub_style": "white"},
        "libx264", tmp_path / "w", frame_size=(320, 240),
        layout={"pw": 320, "ph": 240, "ox": 0, "oy": 0}, duration=4,
    )
    assert _frame_band_mean(out, tmp_path, 1.5) > _frame_band_mean(out, tmp_path, 3.5) + 2.0


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_end_to_end_preview_frame_uses_png(tmp_path):
    src = _make_src(tmp_path)
    out_img = tmp_path / "preview.png"
    core.extract_preview_with_overlays(
        FFMPEG, src, out_img, timestamp=1.0,
        overlays={"burn_subtitles": True, "sub_y": 0.85, "sub_lines": 1,
                  "subtitles_font_size": 16, "sub_style": "yellow"},
        output_ratio="Original", frame_fill="blur",
        work_dir=tmp_path / "pw",
    )
    assert out_img.is_file()
    pngs = list((tmp_path / "pw" / "subtitle_png_cache").glob("sub_*.png"))
    assert pngs
    from PIL import Image

    # Batch CX: the preview shows the sample as ONE line at the
    # locked size — an overflowing sample renders its first fitting
    # piece (the preview is one frame, it has no time window to
    # split across), never a shrunken whole sample.
    assert len(pngs) == 1
    with Image.open(pngs[0]) as im:
        assert im.width <= int(0.92 * 320)
        assert im.height < 40  # one line at the locked size, not two

    band = Image.open(out_img).convert("L").crop((20, 150, 300, 235))
    data = list(band.getdata())
    assert sum(data) / len(data) > 25  # sample subtitle rendered


# ── 11. Cache version ──────────────────────────────────────────────

def test_cache_version_r50():
    assert INDEX_HTML.count("?v=20261113_r143") == 4
    assert "20261104_r48" not in INDEX_HTML
