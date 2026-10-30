"""Subtitle shaping + spelling-consistency tests (owner audit, 2026-10-30).

Owner report: burned-in Myanmar subtitles 'look spelled wrong'. Audit
result (see watermark-v2-2026-10-30.md): every burn path in the app —
main recap ASS, SRT force_style fallback, Live Canvas preview — goes
through libass (HarfBuzz complex-text shaping); there is no drawtext
anywhere in the app. These tests pin that behaviour down:

* canonicalize_burmese_text is a NO-OP on well-formed text (it only
  repairs visual-order typing), so it cannot scramble correct input;
* the spelling-correction tables are exactly what lands in the burned
  ASS (burned text == corrected text == narration text);
* the torture line actually renders (non-blank subtitle band) in every
  bundled font via the real overlay pass.
"""
import shutil
import subprocess

import pytest

from app.services.recap_engine.core import (
    canonicalize_burmese_text,
    resolve_subtitle_font,
    write_synced_ass_file,
)

FFMPEG = shutil.which("ffmpeg")

TORTURE_1 = "ကျေးဇူးတင်ပါတယ်၊ သတ္တိရှိရှိ မန္တလေးမြို့က စာတန်းထိုး စမ်းသပ်ချက်"
TORTURE_2 = "မြန်မာနိုင်ငံ ကျန်းမာရေး ဝန်ကြီးဌာန ကြေညာချက်"


def test_canonicalize_is_noop_on_wellformed_text():
    assert canonicalize_burmese_text(TORTURE_1) == TORTURE_1
    assert canonicalize_burmese_text(TORTURE_2) == TORTURE_2


def test_burned_ass_carries_spelling_corrections(tmp_path):
    # One fix from MYANMAR_SPELLING_FIXES (သံသဝင်) and two from the regex
    # orthography table (ကြေးဇူး, သေခြာ): all must be corrected in the
    # file that actually gets burned, and no wrong form may leak through.
    bad = "သံသဝင်နဲ့ ကြေးဇူးတင်ပါတယ် သေခြာပါတယ်"
    ass = write_synced_ass_file([(0.0, 3.0, bad)], tmp_path / "s.ass", 1280, 720)
    text = ass.read_text(encoding="utf-8")
    assert "သံသယဝင်" in text
    assert "ကျေးဇူး" in text
    assert "သေချာ" in text
    for wrong in ("သံသဝင်", "ကြေးဇူး", "သေခြာ"):
        assert wrong not in text


def test_ass_writer_uses_bundled_family_per_font_option(tmp_path):
    for key in ("default", "serif", "padauk"):
        _, family = resolve_subtitle_font(key)
        ass = write_synced_ass_file(
            [(0.0, 3.0, TORTURE_1)], tmp_path / f"s_{key}.ass", 1280, 720,
            font_name=key,
        )
        assert f"Style: Default,{family}," in ass.read_text(encoding="utf-8")


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
@pytest.mark.parametrize("font_key", ["default", "serif", "padauk"])
def test_torture_line_renders_in_every_font(tmp_path, font_key):
    """Burn the stack-dense torture line through the REAL overlay pass
    (ASS + libass) once per bundled font; the subtitle band must contain
    rendered text pixels. Glyph-level verdict lives in the audit doc —
    this test guards against a font silently resolving to nothing."""
    from app.services.recap_engine.core import apply_production_overlays

    src = tmp_path / "src.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=0x202030:size=1280x720:rate=30:duration=2", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )
    _, family = resolve_subtitle_font(font_key)
    ass = write_synced_ass_file(
        [(0.0, 2.0, TORTURE_1)], tmp_path / "t.ass", 1280, 720,
        font_name=family, raw_font_size=28, sub_lines=1,
    )
    out = tmp_path / "out.mp4"
    layout = {"pw": 1280, "ph": 720, "ox": 0, "oy": 0, "sw": 1280, "sh": 720}
    apply_production_overlays(
        FFMPEG, src, out, {"subtitles_file": str(ass)},
        "libx264", tmp_path / "w", frame_size=(1280, 720), layout=layout,
    )
    frame = tmp_path / "f.png"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", "1.0",
         "-i", str(out), "-frames:v", "1", str(frame)],
        check=True, capture_output=True,
    )
    from PIL import Image

    band = Image.open(frame).convert("L").crop((140, 540, 1140, 700))
    bright = sum(1 for v in band.getdata() if v > 120)
    assert bright > 800, f"{font_key}: subtitle band nearly blank ({bright} px)"
