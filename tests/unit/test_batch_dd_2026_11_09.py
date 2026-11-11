"""Batch DD (2026-11-09): Recap Step-2 (Preview & Settings) rebuild.

Ulfa's approved scope (spec FINAL section + 22:32 parity order —
"output ကိုလဲ preview ကအတိုင်းရအောင်လုပ်နော်"):

* settings groups are a collapsible accordion, ALL collapsed by
  default, headers showing current values;
* the title overlay feature is REMOVED (legacy configs carrying
  title fields still parse — the fields are ignored);
* burn-in label is "စာတန်းထိုး ထည့်မည်";
* 10 style presets + free controls (colours, outline width, bg
  opacity, shadow, bold); custom font upload with built-in
  fallback;
* font size slider 10–26pt and a SIZE-AWARE cue budget;
* Lines 1/2/3 — the budget table extends to 3 lines and the CX/DA
  burn fit wraps into up to the chosen line count;
* blur boxes are a LIST (≤5) of normalised {x,y,w,h,strength},
  preview == render through ONE assembly; legacy single-box
  configs migrate on read;
* anti-copyright is a LEVEL: Off / Standard / Strong (Strong =
  micro speed 0.98–1.02× + pitch + grain + metadata strip);
* cache token r136;
* PARITY: the values the preview shows are the values the renderer
  consumes, from the same normalised inputs and shared constants.
"""
import re
from pathlib import Path

import pytest

import app.services.recap_engine.core as core
import app.services.recap_engine.png_subtitles as pq
import app.services.recap_engine.recap_pipeline as pipeline

ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
API_PY = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")

CW, CH = 1080, 1920
LB = core.frame_layout("9:16", "blur", 1080, 1920)
BOUND = int(0.92 * 1080)

LONG = "မြို့ကြီးဆီကို ထွက်ခွာပြီး သင်္ကြန်ပွဲတော်မှာ ကျောင်းသားတွေ လွှမ်းမိုးနေတဲ့ ပြဿနာကို ဆုံးဖြတ်ခဲ့ကြပါတယ်"


def _renderer(tmp_path):
    return pq.PngSubtitleRenderer(tmp_path / "dd_cache")


def _visuals(font_px, preset="yellow"):
    return pq.preset_visuals(core.SUBTITLE_STYLE_PRESETS[preset], CH, font_px)


# ── 1. Size-aware cue budget ─────────────────────────────────────

def test_budget_pinned_vertical_one_line_values():
    # Batch DJ: the pinned values are the MEASURED capacities —
    # budget(size) = floor(0.95 × median rendered capacity) at the
    # 994 px bound (supersedes the DD fraction-ratio values
    # 56/45/38/32, which overstated capacity ~1.2–1.35×).
    assert core._subtitle_max_chars(True, 1, font_size=13) == 33
    assert core._subtitle_max_chars(True, 1, font_size=16) == 25
    assert core._subtitle_max_chars(True, 1, font_size=19) == 20
    assert core._subtitle_max_chars(True, 1, font_size=22) == 17


def test_budget_monotonic_in_size():
    budgets = [core._subtitle_max_chars(True, 1, font_size=s) for s in (10, 13, 16, 19, 22, 26)]
    assert budgets == sorted(budgets, reverse=True)
    assert len(set(budgets)) == len(budgets)  # strictly decreasing


def test_budget_legacy_values_unchanged_without_size():
    # Batch DJ: without a size, vertical budgets are the measured
    # 16pt-default values (lines × per-line capacity); horizontal
    # keeps the legacy table byte-identically.
    assert core._subtitle_max_chars(True, 1) == 25
    assert core._subtitle_max_chars(False, 1) == 52
    assert core._subtitle_max_chars(True, 2) == 40
    assert core._subtitle_max_chars(False, 2) == 96
    # font_size=16 is the ratio's identity point.
    assert core._subtitle_max_chars(False, 2, font_size=16) == 96


def test_budget_three_line_entries():
    # Batch DJ: vertical 3-line = 3 × the multi-line ladder value.
    assert core._subtitle_max_chars(True, 3) == 60
    assert core._subtitle_max_chars(False, 3) == 144
    # Size-aware values apply to the 3-line entries too.
    assert core._subtitle_max_chars(True, 3, font_size=13) == 75
    assert core._subtitle_max_chars(True, 3, font_size=22) == 39


def test_budget_ratio_applies_to_every_entry():
    for is_vert in (True, False):
        for lines in (1, 2, 3):
            base = core._subtitle_max_chars(is_vert, lines, font_size=16)
            small = core._subtitle_max_chars(is_vert, lines, font_size=13)
            large = core._subtitle_max_chars(is_vert, lines, font_size=22)
            assert small > base > large


# ── 2. Lines 1/2/3 cue splitting + fit wrapping ──────────────────

def test_split_cues_three_lines_stays_one_cue():
    cues = core.split_burmese_subtitle_cues(0.0, 6.0, LONG, max_cue_chars=120, lines_per_cue=3)
    assert len(cues) == 1
    lines = cues[0][2].split("\n")
    assert 2 <= len(lines) <= 3
    assert "".join(lines).replace(" ", "") == LONG.replace(" ", "")


def test_split_cues_two_line_behaviour_unchanged():
    cues = core.split_burmese_subtitle_cues(0.0, 6.0, LONG, max_cue_chars=80, lines_per_cue=2)
    assert len(cues) == 1
    assert len(cues[0][2].split("\n")) == 2


def test_fit_wraps_to_chosen_line_count(tmp_path):
    renderer = _renderer(tmp_path)
    font_px = core.subtitle_font_px(CW, CH, 16, 2, LB)
    font = core.subtitle_font_file("padauk")
    pieces = core.fit_png_cue_pieces(
        renderer, LONG, font_px, font, _visuals(font_px), BOUND, 0.0, 6.0,
        max_lines=2,
    )
    assert pieces, "expected at least one piece"
    # With 2 lines allowed the long cue wraps instead of splitting
    # into sequential one-line pieces: fewer pieces, each ≤2 lines.
    assert all(len(lines) <= 2 for lines, *_ in pieces)
    one_line = core.fit_png_cue_pieces(
        renderer, LONG, font_px, font, _visuals(font_px), BOUND, 0.0, 6.0,
        max_lines=1,
    )
    assert all(len(lines) == 1 for lines, *_ in one_line)
    assert len(pieces) <= len(one_line)
    # No text lost across the wrap.
    rejoined = "".join("".join(lines) for lines, *_ in pieces).replace(" ", "")
    assert rejoined == LONG.replace(" ", "")


def test_fit_default_max_lines_is_cx_behaviour(tmp_path):
    renderer = _renderer(tmp_path)
    font_px = core.subtitle_font_px(CW, CH, 16, 1, LB)
    font = core.subtitle_font_file("padauk")
    pieces = core.fit_png_cue_pieces(
        renderer, LONG, font_px, font, _visuals(font_px), BOUND, 0.0, 6.0
    )
    assert pieces
    assert all(len(lines) == 1 for lines, *_ in pieces)


# ── 3. Multi-blur config: roundtrip, migration, assembly ─────────

def _cfg(**kw):
    from app.webapp.api import RecapConfigRequest

    base = {"url": "https://example.com/v.mp4"}
    base.update(kw)
    return RecapConfigRequest(**base)


def test_multi_blur_config_roundtrip():
    boxes = [
        {"x": 4, "y": 85, "w": 92, "h": 7, "strength": 18},
        {"x": 10, "y": 30, "w": 50, "h": 10, "strength": 30},
    ]
    req = _cfg(blur_boxes=boxes)
    assert [b.model_dump() for b in req.blur_boxes] == boxes
    # Persisted-config roundtrip (model_dump -> parse) is lossless.
    req2 = _cfg(**req.model_dump())
    assert [b.model_dump() for b in req2.blur_boxes] == boxes
    # Fractions conversion (the API -> engine basis) is exact.
    fr = [b.to_fraction_dict() for b in req.blur_boxes]
    assert fr[0] == {"x": 0.04, "y": 0.85, "w": 0.92, "h": 0.07, "strength": 18}
    assert fr[1]["strength"] == 30


def test_blur_box_list_capped_at_five():
    boxes = [{"x": 1, "y": 2, "w": 10, "h": 5}] * 6
    with pytest.raises(Exception):
        _cfg(blur_boxes=boxes)


def test_legacy_single_box_migrates_to_list():
    from app.webapp.api import _dd_blur_boxes_fractions

    req = _cfg(blur_box_explicit=True, blur_y=70, blur_h=9, blur_x=10, blur_w=60, blur_strength=22)
    out = _dd_blur_boxes_fractions(req)
    assert out == [{"x": 0.10, "y": 0.70, "w": 0.60, "h": 0.09, "strength": 22}]
    # No explicit box and no list -> no manual boxes at all.
    assert _dd_blur_boxes_fractions(_cfg()) is None
    # An explicit list wins over the legacy fields.
    req2 = _cfg(blur_box_explicit=True, blur_y=70, blur_h=9,
                blur_boxes=[{"x": 1, "y": 2, "w": 10, "h": 5, "strength": 9}])
    out2 = _dd_blur_boxes_fractions(req2)
    assert len(out2) == 1 and out2[0]["y"] == 0.02


def test_manual_boxes_conversion_matches_detected_band_math():
    # PARITY: a manual box in picture fractions converts through
    # the same layout math as a detected band — one conversion.
    band = {"x": 0.10, "y": 0.30, "w": 0.50, "h": 0.10, "strength": 26}
    via_manual = core.manual_blur_boxes_to_canvas_boxes([band], LB)
    via_band = core.source_band_to_canvas_box(band, LB)
    assert via_manual[0]["x"] == via_band["x"]
    assert via_manual[0]["y"] == via_band["y"]
    assert via_manual[0]["w"] == via_band["w"]
    assert via_manual[0]["h"] == via_band["h"]
    assert via_manual[0]["strength"] == 26


def test_assemble_manual_boxes_alongside_detection():
    plate = {"x": 0.05, "y": 0.85, "w": 0.90, "h": 0.10}
    detected = [{"x": 0.05, "y": 0.30, "w": 0.90, "h": 0.08}]
    manual = [{"x": 0.60, "y": 0.55, "w": 0.30, "h": 0.06, "strength": 33}]
    boxes, covered = pipeline.assemble_blur_boxes(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=detected, manual_boxes=manual,
        layout=LB, canvas_w=CW, canvas_h=CH,
    )
    # Detection still ships (list does NOT suppress it) ...
    assert covered is True
    assert any(abs(b["y"] - 0.30) < 0.05 for b in boxes)
    # ... and the manual box ships VERBATIM (post-merge, AS-4 rule),
    # strength included.
    assert boxes[-1] == manual[0]


def test_assemble_manual_box_plate_yields_on_overlap():
    plate = {"x": 0.05, "y": 0.85, "w": 0.90, "h": 0.10}
    manual = [{"x": 0.10, "y": 0.85, "w": 0.80, "h": 0.09, "strength": 20}]
    boxes, _covered = pipeline.assemble_blur_boxes(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=None, manual_boxes=manual,
        layout=LB, canvas_w=CW, canvas_h=CH,
    )
    assert boxes == manual


# ── 4. Protection levels ─────────────────────────────────────────

def test_protection_level_mapping():
    assert core.normalize_protection_level("bogus") == "standard"
    assert core.normalize_protection_level(None) == "standard"
    assert core.normalize_protection_level("STRONG") == "strong"
    # Off forces the plain frame; Standard/Strong keep the zoom.
    assert core.protection_zoom_factor("off", 1.10) == 1.0
    assert core.protection_zoom_factor("standard", 1.10) == 1.10
    assert core.protection_zoom_factor("strong", 1.06) == 1.06
    # Batch DI: the DD speed factor is REMOVED (owner order — Strong
    # back to pre-DD behaviour, no retime/pitch shift). The function
    # must not exist any more.
    assert not hasattr(core, "protection_speed_factor")


def test_protection_schema_clamps_unknown_to_standard():
    assert _cfg(protection_level="strong").protection_level == "strong"
    assert _cfg(protection_level="weird").protection_level == "standard"


# ── 5. Title removal ─────────────────────────────────────────────

def test_title_absent_from_config_schema():
    from app.webapp.api import RecapConfigRequest, RecapPreviewCompositeRequest

    for model in (RecapConfigRequest, RecapPreviewCompositeRequest):
        fields = set(model.model_fields)
        assert "title_enabled" not in fields
        assert "title_text" not in fields
        assert "title_x" not in fields
        assert "title_y" not in fields
    # The JOB title (display metadata) is a different field — kept.
    assert "title" in RecapConfigRequest.model_fields


def test_legacy_config_with_title_still_parses():
    req = _cfg(title_enabled=True, title_text="ခေါင်းစဉ်", title_x=40, title_y=20,
               title="My Video")
    assert req.title == "My Video"  # job title intact
    assert not hasattr(req, "title_enabled")
    # ... and the persisted dump carries no title-overlay keys.
    dump = req.model_dump()
    assert "title_enabled" not in dump and "title_text" not in dump


def test_title_gone_from_engine_and_frontend():
    core_src = (ROOT / "app/services/recap_engine/core.py").read_text(encoding="utf-8")
    assert "_render_title_png" not in core_src
    assert 'overlays.get("title_text")' not in core_src
    assert "recap-title-toggle" not in INDEX_HTML
    assert "preview-title-box" not in INDEX_HTML
    assert "recapTitleEnabled" not in APP_JS
    assert "title_enabled" not in APP_JS


# ── 6. Fonts: registry, custom fallback, bold ────────────────────

def test_builtin_font_files_exist_and_load():
    from PIL import ImageFont

    assert set(core.SUBTITLE_FONT_OPTIONS) == {"padauk", "noto_sans", "pyidaungsu", "noto_serif"}
    for key in core.SUBTITLE_FONT_OPTIONS:
        path = core.subtitle_font_file(key)
        assert path.is_file(), f"{key} -> {path} missing"
        ImageFont.truetype(str(path), 24)  # loadable by the renderer


def test_custom_font_resolution_and_fallback(tmp_path):
    # A real uploaded font (the bundled Padauk file stands in) wins.
    custom = tmp_path / "myfont.ttf"
    custom.write_bytes(core.subtitle_font_file("padauk").read_bytes())
    got = core.overlays_font_path({"sub_font_custom_path": str(custom), "subtitles_font": "Padauk"})
    assert got == custom
    # Missing file -> built-in fallback, never an error.
    got2 = core.overlays_font_path({"sub_font_custom_path": str(tmp_path / "gone.ttf"),
                                    "subtitles_font": "Padauk"})
    assert got2 == core.subtitle_font_file("padauk")
    # Bold toggle: Regular preferred when off (Noto Sans has both).
    regular = core.subtitle_font_file("noto_sans", bold=False)
    bold = core.subtitle_font_file("noto_sans", bold=True)
    assert "regular" in regular.name.lower()
    assert "bold" in bold.name.lower()


def test_ten_style_presets_registered_everywhere():
    assert len(core.SUBTITLE_STYLE_PRESETS) == 10
    for key in ("rose", "orange", "mint"):
        assert key in core.SUBTITLE_STYLE_PRESETS
        assert f'"{key}"' in API_PY  # prefs enum carries it
        assert f'value="{key}"' in INDEX_HTML  # the dropdown offers it
        assert f"sub_style_{key}" in I18N_JS


def test_visual_overrides_fold_into_png_and_ass():
    preset = core.SUBTITLE_STYLE_PRESETS["yellow"]
    font_px = core.subtitle_font_px(CW, CH, 16, 1, LB)
    overrides = {
        "text_color": "#112233", "outline_color": "#445566",
        "outline_width": 3.0, "bg_color": "#000000", "bg_opacity": 0.5,
        "shadow": True,
    }
    visuals = core.build_subtitle_visuals(preset, overrides, CH, font_px)
    assert visuals["fill"] == (0x11, 0x22, 0x33, 255)
    assert visuals["stroke_colour"] == (0x44, 0x55, 0x66, 255)
    assert visuals["band"] == (0, 0, 0, 128)
    assert visuals["shadow"] is True
    # ASS twin describes the same look.
    merged = core.merge_preset_overrides(preset, overrides)
    assert merged["primary"] == pq.rgba_to_ass_colour((0x11, 0x22, 0x33, 255))
    assert merged["border_style"] == 3
    # No overrides -> the preset itself, untouched.
    assert core.merge_preset_overrides(preset, None) is preset
    base = core.build_subtitle_visuals(preset, None, CH, font_px)
    assert "shadow" not in base or not base["shadow"]


# ── 7. Frontend pins: accordion, label, i18n, slider ─────────────

def test_accordion_groups_present_and_collapsed_by_default():
    # All five DD groups carry the accordion marker; the JS builds
    # headers + hidden bodies (nothing renders open by default).
    for gid in ("recap-preview-ratio-group", "recap-subtitle-group",
                "recap-blur-group", "recap-logo-group",
                "recap-preview-transform-group"):
        assert f'id="{gid}"' in INDEX_HTML
        assert "dd-acc-group" in INDEX_HTML
    assert INDEX_HTML.count("dd-acc-group") >= 5
    assert "setupRecapAccordion" in APP_JS
    assert 'dd-acc-body hidden' in APP_JS  # bodies start hidden
    # The subtitle group is NOT exempted: it is one of the markers.
    sub = INDEX_HTML.index('id="recap-subtitle-group"')
    assert "dd-acc-group" in INDEX_HTML[sub - 60:sub]


def test_burn_label_and_i18n_keys_both_languages():
    assert "စာတန်းထိုး ထည့်မည်" in INDEX_HTML
    # my + en both carry the label and every DD key.
    for key in ("recap_protection_label", "recap_protection_off",
                "recap_protection_standard", "recap_protection_strong",
                "recap_style_custom_label", "recap_font_custom_label",
                "recap_blur_boxes_label", "recap_blur_box_add",
                "recap_sub_pos_label", "sub_lines_three",
                "recap_logo_group_title", "recap_font_upload_btn"):
        assert I18N_JS.count(f"{key}:") >= 2, key
    assert 'recap_burn_subs_label: "📝 စာတန်းထိုး ထည့်မည်"' in I18N_JS
    assert 'recap_burn_subs_label: "📝 Burn Subtitles"' in I18N_JS


def test_font_size_slider_replaces_pills():
    assert 'id="slider-font-size"' in INDEX_HTML
    assert 'min="10" max="26"' in INDEX_HTML
    assert 'id="val-font-size"' in INDEX_HTML
    assert "font-size-picker-toolbar" not in INDEX_HTML
    assert 'data-lines="3"' in INDEX_HTML
    assert 'id="recap-step2-summary"' in INDEX_HTML
    assert 'id="recap-protection-picker"' in INDEX_HTML
    assert 'id="input-sub-font-file"' in INDEX_HTML
    assert 'id="blur-box-chips"' in INDEX_HTML


def test_no_split_notice_anywhere():
    # The CX split notice Ulfa ordered removed must not exist.
    assert "နောက် cue အဖြစ် ခွဲပြပါမည်" not in INDEX_HTML
    assert "နောက် cue အဖြစ် ခွဲပြပါမည်" not in APP_JS


# ── 8. PARITY: preview math == render math ───────────────────────

def test_font_fraction_table_parity_js_vs_core():
    # The Mini App preview's fraction table (subtitleFontFractionOfPicture
    # in app.js) must equal the engine's SUBTITLE_FONT_HEIGHT_FRACTIONS.
    m = re.search(r"const pts = \[\[13, ([\d.]+)\], \[16, ([\d.]+)\], \[19, ([\d.]+)\], \[22, ([\d.]+)\]\]", APP_JS)
    assert m, "JS fraction table not found"
    js = [round(float(m.group(i)) / 100.0, 4) for i in range(1, 5)]
    engine = [frac for _pt, frac in core.SUBTITLE_FONT_HEIGHT_FRACTIONS]
    assert js == engine
    # pt->px is the same function on both sides of the burn: the
    # preview sample and the production cues resolve font_px via
    # subtitle_font_px with the job's line count.
    for lines in (1, 2, 3):
        px = core.subtitle_font_px(CW, CH, 16, lines, LB)
        assert px > 0
    assert core.subtitle_font_px(CW, CH, 16, 3, LB) == core.subtitle_font_px(CW, CH, 16, 2, LB)


def test_preview_and_render_resolve_same_font_and_visuals(tmp_path):
    # Build overlays the way the pipeline builds them (same dict
    # for preview composite and production burn) and assert both
    # resolution paths land on identical font + visuals.
    overlays = {
        "sub_style": "neon",
        "subtitles_font": "Noto Sans Myanmar",
        "subtitles_font_size": 19,
        "sub_lines": 2,
        "sub_visual_overrides": {"text_color": "#ff0000", "shadow": True},
        "sub_bold": False,
    }
    job = core._resolve_png_subtitle_job(
        {**overlays, "subtitle_cues": [[0.0, 2.0, "မင်္ဂလာပါ"]]}, CW, CH,
        {"pw": CW, "ph": CH, "ox": 0, "oy": 0},
    )
    assert job is not None
    assert job["font_path"] == core.overlays_font_path(overlays)
    assert "regular" in job["font_path"].name.lower()  # bold off honoured
    assert job["sub_lines"] == 2
    preset = core.SUBTITLE_STYLE_PRESETS["neon"]
    expected = core.build_subtitle_visuals(
        preset, overlays["sub_visual_overrides"], CH, job["font_px"]
    )
    assert job["visuals"] == expected
    assert job["visuals"]["fill"] == (255, 0, 0, 255)


def test_preview_fit_first_piece_matches_production_first_piece(tmp_path):
    # fit_png_cue_lines (preview) must return the first piece that
    # fit_png_cue_pieces (production) produces, at every line count.
    renderer = _renderer(tmp_path)
    font = core.subtitle_font_file("padauk")
    for lines in (1, 2, 3):
        font_px = core.subtitle_font_px(CW, CH, 16, lines, LB)
        visuals = _visuals(font_px)
        pieces = core.fit_png_cue_pieces(
            renderer, LONG, font_px, font, visuals, BOUND, 0.0, 6.0,
            max_lines=lines,
        )
        first_lines, first_px, first_vis = core.fit_png_cue_lines(
            renderer, LONG, lines, font_px, font, visuals, BOUND
        )
        assert pieces
        assert first_lines == pieces[0][0]
        assert first_px == pieces[0][1]


def test_submit_and_composite_share_dd_payload_fields():
    # One builder feeds both payloads (ddConfigExtras) — no second,
    # divergent mapping on either side.
    assert "function ddConfigExtras()" in APP_JS
    assert APP_JS.count("ddConfigExtras()") >= 3  # def + composite + submit
    assert "...ddConfigExtras()" in APP_JS


# ── 9. Cache token ────────────────────────────────────────────────

def test_cache_token_is_r136():
    assert INDEX_HTML.count("20261104_r136") == 4
    assert "r125" not in INDEX_HTML
    assert INDEX_HTML.count("r136") == 4


def test_dd_sig_declarations_precede_first_use():
    """Regression (live pageerror 2026-11-10): ddChipsSig / ddExtrasSig
    must be declared at the TOP of setupRecapVisualEditor — the
    composite-refresh path reads them during setup, long before the
    blur-list editor section further down the same function. A `let`
    declared there throws "Cannot access before initialization"."""
    app_js = (ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    fn_start = app_js.index("function setupRecapVisualEditor()")
    decl = app_js.index('let ddChipsSig = "";')
    decl2 = app_js.index('let ddExtrasSig = "";')
    refresh = app_js.index("function refreshRecapCompositePreview")
    chips_fn = app_js.index("function ddRenderBlurChips")
    assert fn_start < decl < refresh
    assert fn_start < decl2 < refresh
    assert decl < chips_fn and decl2 < chips_fn
    # Exactly one declaration each (no leftover duplicates below).
    assert app_js.count("let ddChipsSig") == 1
    assert app_js.count("let ddExtrasSig") == 1
