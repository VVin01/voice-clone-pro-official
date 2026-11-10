"""Batch DK (2026-11-10): fix a submit-blocking validation bug —
the prefs layer's outline-width sentinel 422'd every recap submit.

Live root cause (found 2026-11-10 during the admin-account
VoxCPM verification): the prefs layer deliberately allows
``recapOutlineWidth = -1.0`` meaning "the style preset's own
outline" (USER_PREF_FLOATS range is [-1.0, 4.0] and the shipped
default is -1.0). The client forwards the saved value verbatim
into the submit payload (app.js ddConfigExtras:
``out.sub_outline_width = state.recapOutlineWidth``), but both
request schemas declared ``sub_outline_width`` with ``ge=0`` —
so -1 failed validation, FastAPI returned 422, and the app
showed the generic "Recap မစတင်နိုင်ပါ" toast. No job row, no
charge, no app log. ANY account whose saved prefs carry -1
(the default!) was locked out of recap submits entirely on the
flows that restore the value into state.

Fix: both schemas widen to ``ge=-1`` and a field validator
normalises exactly -1 → None ("preset's own" == field absent),
so no downstream consumer (extras builder, persisted config,
worker) ever sees the sentinel; [0, 4] passes through untouched
and < -1 still 422s.

Sibling audit (same class — a pref-legal value that can 422 a
submit): every other pref-forwarded field was compared against
its schema constraint and cleared (crop 0–35, font size 10–30,
lines 1–3, split minutes 1–10, sub margin 0–95, sub_x 10–90,
sub_w 10–100, bg opacity 0–100, zoom unbounded, enums equal to
their validator sets, colours degrade-to-None, voice slugs
curated at the prefs layer). ONE sibling found: the manual
blur-box list — recapBlurBoxesJson is an opaque string at the
prefs layer and the boxes are restored without range checks,
while RecapBlurBoxIn hard-rejected out-of-range geometry.
Following the codebase's clamp-never-reject precedent
(blur_strength / blur_x), the box fields now clamp into their
documented ranges.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.webapp.api import (
    RecapConfigRequest,
    RecapPreviewCompositeRequest,
    _dd_overrides_from,
)

URL = "https://example.com/v.mp4"


def _cfg(**kw):
    base = {"url": URL}
    base.update(kw)
    return RecapConfigRequest(**base)


def _comp(**kw):
    return RecapPreviewCompositeRequest(**kw)


# ── 1. The sentinel normalises on BOTH schemas ───────────────────

@pytest.mark.parametrize("sentinel", [-1, -1.0])
def test_outline_sentinel_normalises_submit(sentinel):
    assert _cfg(sub_outline_width=sentinel).sub_outline_width is None


@pytest.mark.parametrize("sentinel", [-1, -1.0])
def test_outline_sentinel_normalises_composite(sentinel):
    assert _comp(sub_outline_width=sentinel).sub_outline_width is None


# ── 2. Below the sentinel still rejects; the legal range is exact ─

@pytest.mark.parametrize("bad", [-1.5, -2, -100.0])
def test_outline_below_sentinel_rejected_both_models(bad):
    with pytest.raises(ValidationError):
        _cfg(sub_outline_width=bad)
    with pytest.raises(ValidationError):
        _comp(sub_outline_width=bad)


@pytest.mark.parametrize("good", [0, 0.0, 2.2, 4, 4.0])
def test_outline_legal_values_pass_through(good):
    assert _cfg(sub_outline_width=good).sub_outline_width == float(good)
    assert _comp(sub_outline_width=good).sub_outline_width == float(good)


def test_outline_absent_stays_none():
    assert _cfg().sub_outline_width is None
    assert _comp().sub_outline_width is None


def test_outline_above_range_still_rejected():
    with pytest.raises(ValidationError):
        _cfg(sub_outline_width=4.5)


# ── 3. Downstream never sees the sentinel ────────────────────────

def test_extras_omit_outline_width_for_sentinel():
    for req in (_cfg(sub_outline_width=-1), _comp(sub_outline_width=-1)):
        out = _dd_overrides_from(req)
        assert out is None or "outline_width" not in out


def test_extras_carry_real_outline_width():
    out = _dd_overrides_from(_cfg(sub_outline_width=2.2))
    assert out is not None and out["outline_width"] == 2.2


def test_persisted_config_never_carries_sentinel():
    dumped = _cfg(sub_outline_width=-1).model_dump()
    assert dumped["sub_outline_width"] is None
    # And the re-parse of a persisted config is lossless.
    assert _cfg(**dumped).sub_outline_width is None


# ── 4. The live victim's payload shape submits cleanly ──────────
# Built from the submit builder's field list (app.js submit
# payload + ddConfigExtras) with the values an account whose
# saved prefs carry the shipped defaults sends — outline width
# -1.0 included (admin account 1245477389's saved prefs, the
# exact shape that 422'd live on 2026-11-10).

def test_admin_saved_prefs_payload_shape_validates():
    payload = {
        "url": URL,
        "title": None,
        "voice_slug": "komin",
        "story_style": "viral",
        "ratio": "9:16",
        "ratio_fill": "blur",
        "crop_top": 0, "crop_bottom": 0, "crop_left": 0, "crop_right": 0,
        "mirror_video": True,
        "zoom_factor": 1.0,
        "voice_speed": "+12%",
        "blur_subtitles": True,
        "blur_y": 85, "blur_h": 7, "blur_x": 4, "blur_w": 92,
        "blur_box_explicit": False,
        "blur_strength": 20,
        "blur_auto_detect": True,
        "burn_subtitles": True,
        "sub_bottom_margin": 5,
        "sub_x": 50, "sub_w": 92,
        "sub_style": "yellow",
        "sub_font_size": 13,
        "sub_font": "noto_serif",
        "sub_lines": 1,
        "include_logo": True,
        "logo_pos": "top-right",
        # ddConfigExtras for an account on the shipped defaults:
        "blur_boxes": [{"x": 4, "y": 85, "w": 92, "h": 7, "strength": 20}],
        "protection_level": "strong",
        "sub_outline_width": -1.0,          # ← the value that 422'd live
        "sub_bg_opacity": 75,
        "sub_bold": True,
        "duration_seconds": 30,
        "script_review": False,
        "source_language": "auto",
        "render_quality": "max",
        "tts_engine": "voxcpm",
        "gemini_voice": None,
        "voice_ref_id": None,
        "split_video": False,
        "split_minutes": 3,
    }
    req = RecapConfigRequest(**payload)
    assert req.sub_outline_width is None
    out = _dd_overrides_from(req)
    assert out is None or "outline_width" not in out


# ── 5. Sibling audit regression net ──────────────────────────────
# Every pref-legal boundary value (USER_PREF_INTS / FLOATS ranges)
# for a payload-forwarded field must validate on the submit model.

@pytest.mark.parametrize("field,values", [
    ("crop_top", [0, 35]), ("crop_bottom", [0, 35]),
    ("crop_left", [0, 35]), ("crop_right", [0, 35]),
    ("sub_font_size", [10, 30]),
    ("sub_lines", [1, 3]),
    ("split_minutes", [1, 10]),
    ("sub_y", [0, 100]),
    ("sub_bottom_margin", [0, 95]),
    ("sub_x", [10, 90]),
    ("sub_w", [10, 100]),
    ("sub_bg_opacity", [0, 100]),
    ("sub_outline_width", [-1, 0, 4]),
    ("zoom_factor", [1.0, 1.2]),
    ("blur_strength", [4, 40]),
])
def test_pref_boundary_values_validate(field, values):
    for v in values:
        _cfg(**{field: v})


# ── 6. Sibling fix: blur boxes clamp, never reject ───────────────

def test_blur_box_out_of_range_clamps_both_models():
    box = {"x": -5, "y": 140, "w": 0, "h": 250, "strength": 99}
    for req in (_cfg(blur_boxes=[box]), _comp(blur_boxes=[box])):
        b = req.blur_boxes[0]
        assert (b.x, b.y, b.w, b.h, b.strength) == (0.0, 100.0, 1.0, 100.0, 40)


def test_blur_box_strength_floor_clamps():
    b = _cfg(blur_boxes=[{"x": 1, "y": 2, "w": 10, "h": 5, "strength": 1}]).blur_boxes[0]
    assert b.strength == 4


def test_blur_box_in_range_lossless():
    boxes = [
        {"x": 4, "y": 85, "w": 92, "h": 7, "strength": 18},
        {"x": 10, "y": 30, "w": 50, "h": 10, "strength": 30},
    ]
    req = _cfg(blur_boxes=boxes)
    assert [b.model_dump() for b in req.blur_boxes] == [
        {"x": 4.0, "y": 85.0, "w": 92.0, "h": 7.0, "strength": 18},
        {"x": 10.0, "y": 30.0, "w": 50.0, "h": 10.0, "strength": 30},
    ]


def test_blur_box_list_cap_still_five():
    boxes = [{"x": 1, "y": 2, "w": 10, "h": 5}] * 6
    with pytest.raises(ValidationError):
        _cfg(blur_boxes=boxes)
