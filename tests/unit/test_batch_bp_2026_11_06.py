"""Batch BP + BQ (2026-11-06).

BP — crash hotfix. Production jobs rc_1793971254_ec28f9 and
rc_1793971327_71e5e2 (owner's own "Me at the zoo" recaps,
2026-11-06 19:51/19:52 local) failed with the Render-log error
"cannot access local variable '_sub_centre_eff' where it is not
associated with a value". Root cause in run_recap_pipeline: the
effective subtitle centre was computed ONLY inside the
``if blur_subtitles:`` block, but the legacy fallback-box
comparison after that block reads it whenever sub_y or
sub_margin is set — so any job with the master Blur toggle OFF
and a subtitle position/margin set crashed. (Latent since the
Batch AK anchor rework; the preview path computes its
_p_centre_eff unconditionally and never had the bug.) The fix
hoists the anchor computation — byte-identical expressions —
above the blur gate; the blur branch consumes the hoisted
values. The pipeline pins below drive the REAL
run_recap_pipeline at the Batch BI harness seam (fake planner +
fake runner, detector spied out) and observe the centre through
the fallback_box / plate handed to assemble_blur_boxes:
|blur_y - centre| > 0.01 is the only gate, so presence/absence
of the fallback box brackets the centre exactly the way the
production code consumes it.

BQ — the caption-zone rule is narrowed to the BOTTOM zone only
(owner decision 2026-11-06). Batch BM shipped top [0.00, 0.24] /
bottom [0.58, 1.00]; both top-zone bands observed live that day
(the zoo re-run's strip above the subject's head; the
GlunZpix30I crop's [0.000, 0.101] texture band) were scene
texture, never a real caption. Top-of-frame captions are
covered by the user's manual blur box (an explicit box is
authoritative — detection does not run for it). The detector
pins below pin the new drop; the Batch BM/BI/AI pins that
expected top bands to ship were re-pinned in their own files.
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

from core import _subtitle_band_boxes  # noqa: E402
from app.services.recap_engine import recap_pipeline as pipeline_mod  # noqa: E402

PIPELINE_SRC = (
    ROOT / "app/services/recap_engine/recap_pipeline.py"
).read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")


# ── BP harness (the Batch BI render-test seam) ─────────────────────

def _make_tiny_video(path: Path, seconds: int = 2) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size=320x180:rate=15",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(path),
        ],
        check=True,
    )


class _FakeBuilder:
    def __init__(self, **kwargs):
        pass

    def build(self, *, source, job_dir, source_title, reuse=True):
        plan_path = Path(job_dir) / "plan.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text("{}", encoding="utf-8")
        return plan_path, {"segments": []}


class _FakeRunner:
    def __init__(self, **kwargs):
        pass

    def run(self):
        return {
            "final": "x.mp4", "audio": None, "subtitles_srt": None,
            "segments": 0, "duration": 0.0,
        }


def _run_pipeline(tmp_path, monkeypatch, name, **kwargs):
    """Run the real pipeline with planner/runner/detector faked;
    return the kwargs assemble_blur_boxes was called with."""
    src = tmp_path / "tiny.mp4"
    if not src.exists():
        _make_tiny_video(src)
    monkeypatch.setattr(pipeline_mod, "RecapPlanBuilder", _FakeBuilder)
    monkeypatch.setattr(pipeline_mod, "EdgeSmartSync", _FakeRunner)
    monkeypatch.setattr(
        pipeline_mod, "detect_source_subtitle_bands", lambda *a, **k: []
    )
    captured: dict = {}
    real_assemble = pipeline_mod.assemble_blur_boxes

    def spy(**kw):
        captured.update(kw)
        return real_assemble(**kw)

    monkeypatch.setattr(pipeline_mod, "assemble_blur_boxes", spy)
    pipeline_mod.run_recap_pipeline(src, job_dir=tmp_path / name, **kwargs)
    assert captured, "assemble_blur_boxes was never reached"
    return captured


requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not available"
)


# ── BP (a): Blur OFF + sub_y set — the production crash ────────────

@requires_ffmpeg
def test_blur_off_sub_y_set_completes_with_fallback(tmp_path, monkeypatch):
    # The exact production shape: Blur OFF, subtitle position set
    # away from the blur hint. Pre-BP this raised UnboundLocalError
    # at the fallback comparison; now the run completes and the
    # fallback box is built (it is inert downstream — Blur OFF
    # assembles zero boxes — but its construction IS the crash site
    # and its presence proves the comparison ran).
    kw = _run_pipeline(
        tmp_path, monkeypatch, "off_suby",
        blur_subtitles=False, sub_y=0.67, blur_y=0.85,
    )
    assert kw["fallback_box"] == {"x": 0.05, "y": 0.85, "w": 0.90, "h": 0.07}
    assert kw["plate"] is None


@requires_ffmpeg
def test_blur_off_centre_brackets_to_sub_y_anchor(tmp_path, monkeypatch):
    # The centre the comparison uses must be the sub_y-derived
    # anchor (float(sub_y) when sub_margin is None): blur_y within
    # the code's own 0.01 tolerance of sub_y -> no fallback box;
    # outside it -> the box appears.
    kw = _run_pipeline(
        tmp_path, monkeypatch, "off_same",
        blur_subtitles=False, sub_y=0.85, blur_y=0.85,
    )
    assert kw["fallback_box"] is None
    kw = _run_pipeline(
        tmp_path, monkeypatch, "off_within",
        blur_subtitles=False, sub_y=0.67, blur_y=0.675,
    )
    assert kw["fallback_box"] is None  # |0.675 - 0.67| = 0.005 <= 0.01
    kw = _run_pipeline(
        tmp_path, monkeypatch, "off_beyond",
        blur_subtitles=False, sub_y=0.67, blur_y=0.69,
    )
    assert kw["fallback_box"] is not None  # |0.69 - 0.67| = 0.02 > 0.01
    assert kw["fallback_box"]["y"] == 0.69


# ── BP (b): Blur OFF + sub_margin only ─────────────────────────────

@requires_ffmpeg
def test_blur_off_sub_margin_only_completes(tmp_path, monkeypatch):
    # sub_y unset, bottom margin set: the comparison's other
    # trigger. Pre-BP: same UnboundLocalError.
    kw = _run_pipeline(
        tmp_path, monkeypatch, "off_margin",
        blur_subtitles=False, sub_y=None, sub_margin=0.03,
    )
    assert kw["plate"] is None


# ── BP (c): Blur OFF + neither — legacy behaviour unchanged ────────

@requires_ffmpeg
def test_blur_off_neither_sub_y_nor_margin_legacy(tmp_path, monkeypatch):
    # The comparison is never entered (its guard is False), exactly
    # as before the hoist: no fallback box, no plate, run completes.
    kw = _run_pipeline(
        tmp_path, monkeypatch, "off_neither",
        blur_subtitles=False, sub_y=None, sub_margin=None,
    )
    assert kw["fallback_box"] is None
    assert kw["plate"] is None


# ── BP (d): Blur ON — centre values identical to before ────────────

@requires_ffmpeg
def test_blur_on_fallback_gate_unchanged(tmp_path, monkeypatch):
    kw = _run_pipeline(
        tmp_path, monkeypatch, "on_diverged",
        blur_subtitles=True, sub_y=0.67, blur_y=0.85,
    )
    assert kw["fallback_box"] == {"x": 0.05, "y": 0.85, "w": 0.90, "h": 0.07}
    assert kw["plate"] is not None
    kw = _run_pipeline(
        tmp_path, monkeypatch, "on_aligned",
        blur_subtitles=True, sub_y=0.85, blur_y=0.85,
    )
    assert kw["fallback_box"] is None
    assert kw["plate"] is not None


@requires_ffmpeg
def test_blur_on_no_layout_plate_reads_effective_centre(tmp_path, monkeypatch):
    # With no layout the plate fallback exposes the centre
    # directly: plate y IS _sub_centre_eff. The hoist must leave
    # it byte-identical — the bare anchor when no margin refines
    # it, and the blur_y fallback when sub_y is unset.
    monkeypatch.setattr(pipeline_mod, "frame_layout", lambda *a, **k: None)
    kw = _run_pipeline(
        tmp_path, monkeypatch, "on_nolayout",
        blur_subtitles=True, sub_y=0.67, blur_y=0.85,
    )
    assert kw["plate"] == {"x": 0.05, "y": 0.67, "w": 0.90, "h": 0.07}
    kw = _run_pipeline(
        tmp_path, monkeypatch, "on_nolayout_margin",
        blur_subtitles=True, sub_y=None, sub_margin=0.03, blur_y=0.85,
    )
    # The margin refinement needs a layout; without one the centre
    # stays the anchor — float(blur_y) here, as before.
    assert kw["plate"] == {"x": 0.05, "y": 0.85, "w": 0.90, "h": 0.07}


def test_anchor_computed_before_the_blur_gate():
    # Literal pin of the hoist itself: inside run_recap_pipeline
    # the anchor assignment now sits BEFORE the first blur gate,
    # and the blur branch no longer assigns the centre.
    body = PIPELINE_SRC.split("def run_recap_pipeline(", 1)[1].split(
        "\ndef ", 1
    )[0]
    anchor_at = body.index("_sub_centre_eff = _anchor_sub_y")
    gate_at = body.index("if blur_subtitles:")
    assert anchor_at < gate_at
    assert body.count("_sub_centre_eff = _anchor_sub_y") == 1
    assert "if (sub_y is not None or sub_margin is not None) and " \
        "abs(float(blur_y) - float(_sub_centre_eff)) > 0.01:" in body


# ── BQ: the top zone is gone ───────────────────────────────────────

H = 200  # synthetic score-matrix height


def _band(S: np.ndarray, y0: int, rows: int, peak: float) -> None:
    """A dynamic band (alternating strength, dyn ratio ~0.35), the
    Batch BM/BI fixture shape."""
    for k in range(S.shape[0]):
        S[k, y0:y0 + rows] = peak if k % 2 == 0 else peak * 0.7


def _covers(box: dict, lo: float, hi: float) -> bool:
    return box["y"] - box["h"] / 2.0 <= lo and box["y"] + box["h"] / 2.0 >= hi


def test_bq_strong_top_band_is_dropped():
    # A STRONG dynamic band squarely in the old top zone (rows
    # 10-17, padded extent ~0.022-0.114, peak 0.30 — far above
    # every floor): Batch BM shipped it, Batch BQ drops it by
    # position, with the zone log line.
    S = np.zeros((12, H))
    _band(S, 10, 8, 0.30)
    logs: list[str] = []
    assert _subtitle_band_boxes(S, log=logs.append) == []
    assert any("caption-zone" in m for m in logs), logs


def test_bq_top_texture_dropped_bottom_strip_ships():
    # The GlunZpix30I production-crop shape: top texture (rows
    # 0-7, padded extent ~0.000-0.096 — the crop's [0.000, 0.101]
    # band) over the weak genuine bottom strip. Post-BQ only the
    # strip ships; the texture band is zone-dropped.
    S = np.zeros((12, H))
    S[:, :] = 0.004
    _band(S, 0, 8, 0.16)
    for y in range(176, 186):
        S[1, y] = 0.013
        S[7, y] = 0.012
    for k, v in enumerate((0.030, 0.026, 0.033, 0.022, 0.028)):
        for y in range(186, 192):
            S[2 * k, y] = v
    S[9, 189] = 0.024
    logs: list[str] = []
    boxes = _subtitle_band_boxes(S, log=logs.append)
    assert len(boxes) == 1, boxes
    assert _covers(boxes[0], 0.90, 0.955), boxes
    assert any("caption-zone" in m for m in logs), logs


# ── Token ──────────────────────────────────────────────────────────

def test_bp_cache_token_is_r88():
    assert INDEX_HTML.count("20261104_r90") == 4
    assert "20261104_r87" not in INDEX_HTML
