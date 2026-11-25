"""Batch CQ — Telegram Desktop drop-zone drag hint.

On tdesktop the OS file picker never opens from a tap (CL/CP), while
drag-and-drop works. The drop zone carries a hint line that app.js
unhides only when Telegram.WebApp.platform === 'tdesktop'.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
INDEX = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")


def test_hint_element_exists_hidden_by_default():
    m = re.search(r'<span class="drop-desktop-hint" id="recap-drop-desktop-hint"[^>]*>', INDEX)
    assert m, "desktop hint span missing from the drop zone"
    assert 'data-i18n="recap_drop_desktop_hint"' in m.group(0)
    assert "hidden" in m.group(0)
    # inside the drop-zone label (after the limits line)
    label = INDEX[INDEX.index('class="drop-zone-content"'):INDEX.index('id="recap-upload-progress"')]
    assert "recap-drop-desktop-hint" in label
    assert label.index("recap_choose_video_file") < label.index("recap-drop-desktop-hint")


def test_hint_key_in_both_locales():
    assert I18N_JS.count("recap_drop_desktop_hint") == 2
    assert "ဆွဲထည့်ပါ" in I18N_JS
    assert "drag & drop the video file here" in I18N_JS


def test_gate_only_tdesktop():
    assert "function applyDesktopDropHint()" in APP_JS
    body = APP_JS[APP_JS.index("function applyDesktopDropHint()"):][:600]
    assert 'Telegram.WebApp.platform' in body
    assert '"tdesktop"' in body
    assert 'getElementById("recap-drop-desktop-hint")' in body
    assert "hint.hidden = false" in body
    assert "applyDesktopDropHint();" in APP_JS  # called from init


def test_hint_style_present():
    m = re.search(r"\.drop-desktop-hint \{[^}]*\}", CSS)
    assert m and "font-weight: 600" in m.group(0)


def test_cp_overlay_untouched():
    assert 'class="drop-zone-file-input"' in INDEX
    assert ".drop-zone-file-input {" in CSS


def test_cache_token_r113():
    assert INDEX.count("20261125_admin_r23") == 5
