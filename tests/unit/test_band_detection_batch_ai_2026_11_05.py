"""Batch AI — source subtitle band detection fix (2026-11-05).

The old detector aggregated per-row glyph scores with a MEAN across
sampled frames and gated every row on pers >= 0.35 / agg >= 0.012 /
group peak >= 0.03. Proven miss on the real recap source GlunZpix30I:
the caption strip at ~0.91 of the picture scored pers 0.33 and
mean-agg 0.015 per row (dialogue is intermittent; one-line and
two-line captions put glyphs on different rows), so it never formed a
band, while the static title card mid-picture (pers 1.00, agg 0.174,
identical in every sample) was the only band returned — the delivered
video shipped with the source's English captions uncovered as
mirror-writing at the bottom of the picture.

Batch AI (core._subtitle_band_boxes):
* temporal aggregate = 75th percentile (strength when present),
  persistence keeps measuring how often;
* two-tier candidates — strict rows seed a band, loose rows extend
  it, so evidence spread across neighbouring glyph rows pools;
* bands whose glyph pattern never changes between samples are
  static overlays (title cards / banners / watermarks) and are
  dropped when a genuinely changing dialogue band was found; when
  every band is static, all are kept (looped-image sources).
  Batch BM amendment (owner Decision 1, 2026-11-06): the
  caption-zone rule governs static bands too — an all-static
  source's band is kept only when it sits in a caption zone;
  a mid-picture static band is zone-dropped like any other.

Unit tests drive _subtitle_band_boxes with synthetic score matrices;
video tests reuse the synthetic-clip pattern from
test_round4_2026_11_01.py; one test re-proves the fix against the
actual GlunZpix30I delivered file when it is present on this machine
(it lives outside the repo, so the test skips elsewhere — the live
run is also recorded in the Batch AI PR).

Live-run record (2026-11-05, this machine, detector as shipped here):
* delivered file (1080x1920): boxes y=0.49 h=0.16 (the Myanmar burn-in
  carried in that copy) and y=0.61 h=0.07 covering 0.571-0.646 — the
  real caption strip (core 0.604-0.630) is covered; the two static
  title-card bands (y=0.33, y=0.37) are ignored with a log line.
  The OLD detector on the same file returned the title bands and
  rejected the caption group as a "weak smear" (peak 0.0154 < 0.03).
* picture-cropped copy (1080x608, the production geometry): boxes
  y=0.52 h=0.16 (the burn-in) and y=0.93 h=0.10 covering 0.882-0.980 —
  the caption strip (core 0.877-0.959) is covered; three static
  bands ignored.

Batch BM re-run record (2026-11-06, same machine): Batch BI's
prominence gate had silently broken the cropped run — the strip's
group died at the gate (peak 0.0306 vs floor 0.0889; see the BL
drift investigation, fix-bl-work/report-build.md) while the
canvas-geometry test below stayed green. Batch BM removed the
gate, added the caption-zone drop (before the cap), and
re-geometryed the real-file test to the cropped copy — the
production geometry above. Detector on the crop now returns
y=0.0507 h=0.1013 (top texture, in-zone) and y=0.9375 h=0.0987
(extent 0.888-0.987) covering the strip's main-line glyphs
(0.934-0.967 measured in the sampled frames; strip core
0.877-0.959 per the algebra in the pre-check record).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app" / "services" / "recap_engine"))

from core import _subtitle_band_boxes, detect_source_subtitle_bands  # noqa: E402

H = 200  # synthetic score-matrix height


def _matrix(n: int = 12) -> np.ndarray:
    return np.zeros((n, H))


def _covers(box: dict, lo: float, hi: float) -> bool:
    return box["y"] - box["h"] / 2.0 <= lo and box["y"] + box["h"] / 2.0 >= hi


def _caption_matrix() -> np.ndarray:
    """The GlunZpix30I failure profile, distilled: a bottom caption
    band whose evidence is spread over rows 176-191 (0.88-0.96) —
    weak upper-line rows present in 2/12 samples, a main line whose
    best row crosses the hit score in 5/12, everything over a low
    ever-present glyph-noise floor."""
    S = _matrix()
    S[:, :] = 0.004
    # upper line of two-line captions: 2 samples, modest scores
    for y in range(176, 186):
        S[1, y] = 0.013
        S[7, y] = 0.012
    # main line: 5 dialogue samples with varying text strength
    for k, v in enumerate((0.030, 0.026, 0.033, 0.022, 0.028)):
        for y in range(186, 192):
            S[2 * k, y] = v
    # one row of the main line also catches a shorter cue (seed row:
    # 6 hits -> pers 0.50 once the noise floor frames are counted)
    S[9, 189] = 0.024
    return S


def test_intermittent_bottom_caption_is_found():
    boxes = _subtitle_band_boxes(_caption_matrix())
    assert boxes, "the GlunZpix30I caption profile must produce a band"
    assert any(_covers(b, 0.90, 0.955) for b in boxes), boxes


def test_static_title_card_dropped_when_dialogue_exists():
    S = _caption_matrix()
    # static title card mid-picture: identical strong scores always
    S[:, 80:96] = 0.174
    boxes = _subtitle_band_boxes(S)
    assert boxes
    assert any(_covers(b, 0.90, 0.955) for b in boxes), boxes
    assert not any(_covers(b, 0.42, 0.44) for b in boxes), boxes


def test_all_static_bands_are_kept():
    # A looped-image / slideshow source: the only band never changes.
    # It must still be returned (pre-AI behaviour for static sources).
    # Batch BM (owner Decision 1, 2026-11-06): the caption-zone rule
    # governs static bands too — NO static exemption. An in-zone
    # static band is kept exactly as Batch AI designed...
    S = _matrix()
    S[:, 176:192] = 0.174  # static band over the caption rows
    boxes = _subtitle_band_boxes(S)
    assert len(boxes) == 1
    assert _covers(boxes[0], 0.90, 0.955)
    # ...but the same static band mid-picture (the pre-BM fixture:
    # rows 80-95, extent 0.328-0.488) is now zone-dropped — a static
    # overlay in the middle of the picture is not blurred either.
    S_mid = _matrix()
    S_mid[:, 80:96] = 0.174
    assert _subtitle_band_boxes(S_mid) == []


def test_hairline_still_rejected():
    # Two very strong rows and nothing else: extent 0.01 < 0.015.
    S = _matrix()
    S[:, 100:102] = 0.5
    assert _subtitle_band_boxes(S) == []


def test_loose_smear_without_seed_forms_no_band():
    # Broad but weak-and-rare evidence, no row ever strict.
    S = _matrix()
    for k in range(3):  # 3/12 samples -> pers 0.25
        S[k, 150:170] = 0.009
    assert _subtitle_band_boxes(S) == []


def test_no_text_no_band():
    assert _subtitle_band_boxes(_matrix()) == []
    assert _subtitle_band_boxes(np.zeros((0, H))) == []


# ── video-level tests (synthetic clips) ─────────────────────────────


def _frame_png(path: Path, *, title: bool, caption: str | None) -> None:
    from PIL import Image

    img = Image.new("L", (320, 240), 40)
    px = img.load()
    if title:
        for y in range(84, 104):
            for x in range(320):
                px[x, y] = 235 if (x // 3) % 2 == 0 else 25
    if caption == "a":  # dense stripes
        for y in range(216, 232):
            for x in range(320):
                px[x, y] = 235 if (x // 3) % 2 == 0 else 25
    elif caption == "b":  # wider stripes -> different row scores
        for y in range(216, 232):
            for x in range(320):
                px[x, y] = 235 if (x // 12) % 2 == 0 else 25
    img.save(path)


def _segment_clip(tmp_path: Path, name: str, segments: list[tuple[bool, str | None]]) -> Path:
    """Build a clip from (title, caption-variant) segments of 3 s each."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    seg_files = []
    for i, (title, caption) in enumerate(segments):
        png = tmp_path / f"{name}_seg{i}.png"
        _frame_png(png, title=title, caption=caption)
        seg = tmp_path / f"{name}_seg{i}.mp4"
        res = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-loop", "1", "-i", str(png),
             "-t", "3", "-pix_fmt", "yuv420p", "-r", "10", str(seg)],
            capture_output=True, text=True, timeout=60,
        )
        assert res.returncode == 0, res.stderr
        seg_files.append(seg)
    list_file = tmp_path / f"{name}_list.txt"
    list_file.write_text("".join(f"file '{p}'\n" for p in seg_files), encoding="utf-8")
    out = tmp_path / f"{name}.mp4"
    res = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(list_file), "-c", "copy", str(out)],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    return out


def test_video_dynamic_captions_found_static_title_ignored(tmp_path):
    from recap_pipeline import find_binary

    ffmpeg, ffprobe = find_binary("ffmpeg"), find_binary("ffprobe")
    # Title card in every segment (static); bottom captions change
    # pattern between segments and vanish in one — like real dialogue.
    clip = _segment_clip(tmp_path, "dyn", [(True, "a"), (True, "b"), (True, None), (True, "a")])
    boxes = detect_source_subtitle_bands(ffmpeg, ffprobe, clip, tmp_path / "w", log=lambda m: None)
    assert any(_covers(b, 0.90, 0.955) for b in boxes), boxes
    assert not any(_covers(b, 0.375, 0.415) for b in boxes), boxes


def test_video_text_free_clip_returns_nothing(tmp_path):
    from recap_pipeline import find_binary

    ffmpeg, ffprobe = find_binary("ffmpeg"), find_binary("ffprobe")
    clip = _segment_clip(tmp_path, "clean", [(False, None), (False, None)])
    boxes = detect_source_subtitle_bands(ffmpeg, ffprobe, clip, tmp_path / "w", log=lambda m: None)
    assert boxes == []


# ── the real file (documented live run; skips off this machine) ────

REAL_FILE = (
    Path.home()
    / "workspace/user/media_library/video/3a/3a7c977f26b1bc59ac2f26a4526f7a6c4e6072b27ce02da3820d76566466cb33.mp4"
)


@pytest.mark.skipif(not REAL_FILE.is_file(), reason="GlunZpix30I delivered copy not on this machine")
def test_real_glunzpix30i_caption_strip_covered(tmp_path):
    from recap_pipeline import find_binary

    ffmpeg, ffprobe = find_binary("ffmpeg"), find_binary("ffprobe")
    # Batch BM (owner Decision 3, 2026-11-06): detect on the
    # PRODUCTION geometry — the 1080x608 source picture cropped out
    # of the delivered 1080x1920 canvas (top offset 626, re-verified
    # empirically in the BL drift investigation) — never on the
    # letterboxed canvas, whose empty rows distorted every
    # frame-relative statistic and hid the Batch BI prominence-gate
    # regression this batch removes. All fractions below are
    # SOURCE-picture fractions.
    crop = tmp_path / "glunzpix30i_picture.mp4"
    res = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", str(REAL_FILE),
         "-vf", "crop=1080:608:0:626", "-an", str(crop)],
        capture_output=True, text=True, timeout=600,
    )
    assert res.returncode == 0, res.stderr
    boxes = detect_source_subtitle_bands(ffmpeg, ffprobe, crop, tmp_path / "w", log=lambda m: None)
    # The genuine caption strip (core 0.877-0.959 in source
    # fractions; main-line glyphs measured at 0.934-0.967 in the
    # sampled frames) must be covered by a returned band. Measured
    # post-BM output: y=0.9375 h=0.0987 (extent 0.888-0.987).
    assert any(_covers(b, 0.90, 0.96) for b in boxes), boxes
    assert any(_covers(b, 0.934, 0.967) for b in boxes), boxes
    # Every returned band must lie entirely inside a caption zone —
    # the Myanmar burn-in this delivered copy carries (~0.44-0.60)
    # and mid-picture scene texture are zone-dropped, and the
    # static title card must not come back either.
    for b in boxes:
        lo, hi = b["y"] - b["h"] / 2.0, b["y"] + b["h"] / 2.0
        assert (lo >= 0.0 and hi <= 0.24) or (lo >= 0.58 and hi <= 1.0), boxes
