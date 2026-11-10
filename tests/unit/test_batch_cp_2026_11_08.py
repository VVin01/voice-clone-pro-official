"""Batch CP (2026-11-08): Recap upload on Telegram Desktop (CL round 2).

Owner verdict (2026-11-08 01:53): on the Telegram DESKTOP app, tapping
the Recap Step-1 drop zone does nothing — the file picker never opens.
CL's native <label for> activation is swallowed by the Desktop webview
(the forwarded synthetic click lacks trusted user activation there),
while Chromium and phone webviews honor it.

CP makes the tap land DIRECTLY on the file input: #recap-video-file-input
becomes a full-cover invisible overlay of the drop zone
(.drop-zone-file-input; the zone gains position: relative). The label
stays as the zone's content. Because the upload-progress UI (incl. the
Cancel button) lives inside the zone, setRecapPreviewState disables the
overlay's pointer events in every state except "empty".

The other file inputs (logo, ref audio, own voice, slip) are untouched —
Desktop was only reported broken for the recap drop zone.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")

ACCEPT = ('accept="video/mp4,video/x-matroska,video/quicktime,video/x-msvideo,'
          'video/webm,video/mp2t,video/x-m4v,.mp4,.mkv,.mov,.avi,.webm,.ts,.m4v"')


def test_cp_recap_input_is_the_overlay():
    tag = re.search(r'<input[^>]*id="recap-video-file-input"[^>]*>', HTML)
    assert tag is not None
    assert 'class="drop-zone-file-input"' in tag.group(0)
    assert 'class="hidden-file-input"' not in tag.group(0)
    assert ACCEPT in tag.group(0)  # accept list unchanged


def test_cp_overlay_css_and_relative_zone():
    rule = re.search(r"\.drop-zone-file-input\s*\{([^}]*)\}", CSS)
    assert rule is not None
    body = rule.group(1)
    for needle in ("position: absolute", "inset: 0", "width: 100%",
                   "height: 100%", "opacity: 0", "cursor: pointer"):
        assert needle in body, needle
    zone = re.search(r"\.video-drop-zone\s*\{([^}]*)\}", CSS)
    assert zone is not None and "position: relative" in zone.group(1)


def test_cp_label_content_retained():
    assert '<label class="drop-zone-content" for="recap-video-file-input">' in HTML
    # Still no JS click chain for this input (CL's removal stands).
    assert 'getElementById("recap-video-file-input").click()' not in APP_JS
    assert "getElementById('recap-video-file-input').click()" not in APP_JS


def test_cp_pointer_events_gated_by_preview_state():
    fn = APP_JS.split("function setRecapPreviewState(mode) {", 1)[1].split("\n}", 1)[0]
    assert 'document.getElementById("recap-video-file-input")' in fn
    assert 'dzFileInput.style.pointerEvents = mode === "empty" ? "" : "none"' in fn


def test_cp_other_inputs_untouched():
    for fid in ("input-logo-file", "ref-audio-input",
                "recap-own-voice-input", "slip-file-input"):
        tag = re.search(rf'<input[^>]*id="{fid}"[^>]*>', HTML)
        assert tag is not None and 'class="hidden-file-input"' in tag.group(0), fid


def test_cp_cache_token_is_r112():
    assert HTML.count("20261104_r126") == 4
    assert ("20261104_" + "r111") not in HTML
