"""Recap settings-UI fix batch (2026-11-02) — RS-1/RS-2/RS-6/RS-7.

Pins for the audit-driven fixes in the Recap tab's detailed settings
(see recap-settings-audit-2026-11-01/INVENTORY.md):

- RS-1: multi-row pill-group tracks get a concentric-safe radius and
  the active pill's paint (outer shadow) cannot leave the track.
- RS-6/RS-7: the wrap-prone Recap pickers are equal-height grids
  (grid-auto-rows:1fr) with column counts that divide their options
  evenly; even heights are additionally proven by DOM measurement in
  the fix record (the pytest stack is static-only).
- RS-2: the canvas subtitle SAMPLE preview is never line-clamped.
- RS-7 minors: seek panel lines both span the full width; the
  crop-sync chip label stays on one line; the Mirror control carries
  a single label (row label above, state-only text in the toggle).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
CSS = (STATIC / "css" / "style.css").read_text(encoding="utf-8")

RS_BLOCK_MARKER = "Recap settings-UI fix batch (2026-11-02"


def _rs_block() -> str:
    i = CSS.index(RS_BLOCK_MARKER)
    return CSS[i:]


def test_rs_block_present():
    assert RS_BLOCK_MARKER in CSS


def test_rs1_track_radius_is_concentric_safe():
    block = _rs_block()
    assert ".pill-group, .pill-group.sm { border-radius: 24px; }" in block


def test_rs1_active_pill_paint_stays_inside_track():
    block = _rs_block()
    # Inside a track the active pill keeps only its inset specular
    # shadow; the outer drop shadow is what could paint past the
    # track border.
    assert ".pill-group .pill-btn.active { box-shadow: var(--glass-hi); }" in block


def test_rs6_pickers_are_equal_height_grids():
    block = _rs_block()
    for pid in ("#recap-quality-picker", "#recap-logo-pos-picker",
                "#recap-fill-picker", "#recap-crop-presets-toolbar",
                "#sub-lines-picker-toolbar", "#font-size-picker-toolbar",
                "#recap-sub-font-picker", "#recap-speed-picker",
                "#sub-style-picker", "#story-style-picker-quick"):
        assert f"{pid}" in block, pid
    # One shared mechanism: grid rows sized 1fr so every option in a
    # picker shares the tallest option's height.
    assert block.count("grid-auto-rows: 1fr") >= 10
    assert ".ratio-pills-grid { grid-auto-rows: 1fr; }" in block


def test_rs7_column_counts_divide_evenly():
    block = _rs_block()
    # Size: 4 options, one even row (was 3+1).
    assert "#font-size-picker-toolbar { grid-template-columns: repeat(4, 1fr)" in block
    # Logo Position: 4 options as 2x2.
    assert "#recap-logo-pos-picker { grid-template-columns: repeat(2, 1fr)" in block
    # Subtitle Style: 7 chips in 2 columns — the 7th is a normal cell,
    # no longer stretched full-width by flex-grow.
    assert "#sub-style-picker {" in block and "repeat(2, 1fr)" in block
    # Speed: 5 options as 3+2 across a 6-column grid.
    assert "#recap-speed-picker { grid-template-columns: repeat(6, 1fr)" in block
    assert "#recap-speed-picker .pill-btn { grid-column: span 2; }" in block
    assert "#recap-speed-picker .pill-btn:nth-child(4)" in block
    # Fill: the tab's longest labels stack full-width, one line each.
    assert "#recap-fill-picker { grid-template-columns: 1fr;" in block
    assert "#recap-fill-picker .pill-btn { white-space: nowrap; }" in block


def test_rs6_fine_pickers_not_converted():
    block = _rs_block()
    # Verified-FINE in the audit — the fix must not churn them.
    for pid in ("#recap-engine-picker", "#recap-lang-picker",
                "#recap-split-minutes-picker", "#recap-zoom-picker"):
        assert pid not in block, pid


def test_rs2_subtitle_sample_never_clamped():
    block = _rs_block()
    assert ".preview-sub-box.one-line .preview-sub-text" in block
    assert ".preview-sub-box.two-lines .preview-sub-text" in block
    assert "display: block; overflow: visible;" in block
    # The Lines setting itself still drives the real render payload.
    assert "sub_lines: state.recapLines || 1" in APP_JS


def test_rs7_seek_panel_one_packing_rule():
    block = _rs_block()
    assert ".seek-actions-row .mini-btn-seek { flex: 1 1 0; }" in block
    assert ".seek-presets-pills {" in block
    assert "margin-left: 0; justify-content: space-between;" in block


def test_rs7_crop_sync_chip_single_line():
    block = _rs_block()
    assert "#btn-sync-crop { flex: 0 0 auto; }" in block
    assert "#btn-sync-crop .chip-lbl { white-space: nowrap; }" in block
    assert 'id="btn-sync-crop"' in HTML


def test_rs7_mirror_single_label():
    # The row label above the toggle names the control; the toggle's
    # own status text now carries only the state (no second "Mirror").
    assert 'data-i18n="recap_mirror_label"' in HTML
    assert 'data-i18n="mirror_on">ON ✅</span>' in HTML
    assert 'mirror_on: "ဖွင့် ✅"' in I18N_JS
    assert 'mirror_off: "ပိတ် ❌"' in I18N_JS
    assert 'mirror_on: "ON ✅"' in I18N_JS
    assert 'mirror_off: "OFF ❌"' in I18N_JS
    assert "Mirror: ON" not in I18N_JS
    assert "Mirror: ဖွင့်" not in I18N_JS


def test_cache_buster_bumped_for_settings_ui():
    assert HTML.count("?v=20261102_ga1") == 3
    assert "?v=20261102_rr1" not in HTML
