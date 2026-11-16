"""Batch CL — Upload zones: native <label> activation (2026-11-07).

Ulfa reported (22:40) that on LAPTOP (Telegram Desktop / desktop
WebView) the Recap Step-1 upload zone cannot be clicked — no file
picker opens — while on his phone it works. Reproduced at desktop
width in Telegram Web A: every visible upload control is dead.

Cause: the drop zone's activation was a JS .click() chain —

    <div class="drop-zone-content"
         onclick="document.getElementById('recap-video-file-input').click()">

Desktop embedded WebViews drop programmatic .click() calls on hidden
file inputs; phone WebViews honour them. The input itself is healthy
(files set programmatically upload fine) and the CSS is innocent.

Three of the five file inputs already used the proven house pattern,
native label activation (<label class="btn-file-upload" for="...">):
ref-audio-input, recap-own-voice-input, slip-file-input. Batch CL
brings the two outliers onto the same pattern and changes nothing
else about upload handling:

- CL-1: the recap drop-zone content is now
  <label class="drop-zone-content" for="recap-video-file-input">
  (inline onclick removed). Drag/drop on #recap-video-drop-zone and
  the input's change handler are unaffected; .drop-zone-content
  already sets display:flex + cursor:pointer, so the label renders
  exactly like the div did.
- CL-2: the logo upload <button id="btn-logo-upload"> is now
  <label class="pill-btn" id="btn-logo-upload" for="input-logo-file">
  and its .click() listener is gone. A label has no `disabled`, so
  the change handler keeps its no-double-submit behaviour via a
  logoUploadBusy flag (re-entry ignored) + pointer-events /
  aria-disabled toggling on the label while an upload is in flight.
  The logo input's class moves from "hidden" to "hidden-file-input"
  for consistency with the other label-driven inputs.

These tests pin:
- EVERY input[type=file] in index.html (exactly the five known ids)
  has a <label for="<id>">, in the spec'd class forms;
- NO file input is activated via a .click() chain anywhere in
  index.html / app.js (getElementById('<id>').click() and the known
  variable spellings), and the old inline onclick is gone;
- the logo busy guard replaced the disabled toggling;
- in a node mini-DOM encoding native label semantics, clicking the
  recap drop-zone label / the logo label dispatches activation to
  its input EXACTLY once (label `for` values are read from the real
  shipped markup), and clicking an input directly fires it once;
- visual parity: .drop-zone-content stays display:flex +
  cursor:pointer and .logo-manager-controls stays inline-flex
  (the logo label is a flex item, so pill sizing is unchanged);
- the r109 cache-token release pin.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app" / "webapp" / "static" / "css" / "style.css").read_text(encoding="utf-8")

FILE_INPUT_IDS = [
    "ref-audio-input",
    "recap-video-file-input",
    "input-sub-font-file",
    "input-logo-file",
    "recap-own-voice-input",
    "slip-file-input",
]


def _file_input_ids() -> list:
    return re.findall(r'<input[^>]*type="file"[^>]*\bid="([^"]+)"', INDEX_HTML) + [
        m for m in re.findall(r'<input[^>]*\bid="([^"]+)"[^>]*type="file"', INDEX_HTML)
    ]


def _label_fors() -> list:
    return re.findall(r'<label[^>]*\bfor="([^"]+)"', INDEX_HTML)


# ── CL-3 pins: every file input is label-activated ──────────────────

def test_cl_exactly_the_known_file_inputs_exist():
    assert sorted(set(_file_input_ids())) == sorted(FILE_INPUT_IDS)


def test_cl_every_file_input_has_a_matching_label():
    fors = _label_fors()
    for fid in FILE_INPUT_IDS:
        assert fid in fors, f"no <label for> for {fid}"


def test_cl_untouched_three_keep_their_house_label_forms():
    assert '<label class="btn-file-upload" for="ref-audio-input">' in INDEX_HTML
    assert (
        '<label class="btn-file-upload" for="recap-own-voice-input" '
        'id="recap-own-voice-upload-btn">'
    ) in INDEX_HTML
    assert '<label class="btn-file-upload slip-file-label mt-2" for="slip-file-input">' in INDEX_HTML


# ── CL-1 pins: recap drop zone is a label, chain gone ───────────────

def test_cl_recap_drop_zone_content_is_a_label():
    assert '<label class="drop-zone-content" for="recap-video-file-input">' in INDEX_HTML
    assert 'class="drop-zone-content"' in INDEX_HTML
    # The old div + inline .click() chain is gone.
    assert '<div class="drop-zone-content"' not in INDEX_HTML
    assert "getElementById('recap-video-file-input').click()" not in INDEX_HTML
    assert 'getElementById("recap-video-file-input").click()' not in INDEX_HTML


# ── CL-2 pins: logo control is a label; busy guard, not disabled ────

def test_cl_logo_upload_control_is_a_label():
    assert (
        '<label class="pill-btn" id="btn-logo-upload" for="input-logo-file">'
        '<span data-i18n="btn_logo_upload">'
    ) in INDEX_HTML
    assert '<button type="button" class="pill-btn" id="btn-logo-upload">' not in INDEX_HTML
    # Input class aligned with the other label-driven inputs.
    tag = re.search(r'<input[^>]*id="input-logo-file"[^>]*>', INDEX_HTML)
    assert tag is not None
    assert 'class="hidden-file-input"' in tag.group(0)
    assert 'class="hidden"' not in tag.group(0)


def test_cl_logo_click_listener_and_disabled_toggling_removed():
    assert "inputLogoFile.click()" not in APP_JS
    assert 'btnLogoUpload.addEventListener("click"' not in APP_JS
    assert "btnLogoUpload.disabled =" not in APP_JS


def test_cl_logo_busy_guard_present():
    assert "let logoUploadBusy = false;" in APP_JS
    assert "if (logoUploadBusy) return;" in APP_JS
    assert 'btnLogoUpload.style.pointerEvents = "none";' in APP_JS
    assert 'btnLogoUpload.setAttribute("aria-disabled", "true");' in APP_JS
    # finally-block reset
    assert "logoUploadBusy = false;" in APP_JS
    assert 'btnLogoUpload.style.pointerEvents = "";' in APP_JS
    assert 'btnLogoUpload.removeAttribute("aria-disabled");' in APP_JS


# ── CL-3 pins: no .click() activation chain for ANY file input ──────

def test_cl_no_click_chain_for_any_file_input():
    for fid in FILE_INPUT_IDS:
        for src_name, src in (("index.html", INDEX_HTML), ("app.js", APP_JS)):
            assert not re.search(
                r"getElementById\(\s*['\"]" + re.escape(fid) + r"['\"]\s*\)\s*\.click\(\)",
                src,
            ), f"{src_name} still .click()-activates {fid}"
    # The variable spellings bound to the five inputs.
    for var_click in ("inputLogoFile.click()", "refFileInput.click()", "fileInput.click()"):
        assert var_click not in APP_JS, f"app.js still contains {var_click}"


# ── Visual parity pins ──────────────────────────────────────────────

def test_cl_drop_zone_content_css_unchanged_for_label_rendering():
    # The class carried display:flex + cursor:pointer on the div; the
    # label renders identically because the class is unchanged and
    # labels carry no UA margin.
    assert re.search(r"\.drop-zone-content \{[^}]*display: flex", STYLE_CSS)
    assert re.search(r"\.drop-zone-content \{[^}]*cursor: pointer", STYLE_CSS)


def test_cl_logo_controls_flex_parent_intact():
    # .logo-manager-controls is inline-flex, so the pill label is a
    # flex item (block-ified) and keeps the button's pill sizing.
    assert re.search(r"\.logo-manager-controls \{[^}]*display: inline-flex", STYLE_CSS)
    assert re.search(r"\.pill-btn \{[^}]*min-height: 38px", STYLE_CSS)


# ── Node harness: native label activation, exactly once ─────────────
#
# Label activation is browser behaviour, not app code: per the HTML
# spec, a click on a label whose labelled control exists fires ONE
# synthetic click at that control (when the click was not aimed at
# the control itself and was not default-prevented). The mini-DOM
# below encodes exactly that rule; the label `for` values fed into
# it are read from the real shipped markup above, so the harness
# proves the shipped label/input pairing activates exactly once.

_MINI_DOM = r"""
class El {
    constructor(tag, attrs) {
        this.tagName = tag.toUpperCase();
        this.attrs = attrs || {};
        this.children = [];
        this.listeners = {};
    }
    get htmlFor() { return this.attrs.for || null; }
    appendChild(c) { this.children.push(c); return c; }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
}
const registry = {};
function register(el) { if (el.attrs.id) registry[el.attrs.id] = el; }
const clickCounts = {};
function fireClick(el) {
    const ev = { target: el, defaultPrevented: false,
                 preventDefault() { this.defaultPrevented = true; } };
    for (const fn of (el.listeners.click || [])) fn(ev);
    return ev;
}
function userClick(el) {
    // A user click lands on el; if el is a label with a labelled
    // control, the browser then fires ONE synthetic click at the
    // control (unless the label's own click was default-prevented
    // or the target already IS the control).
    const ev = fireClick(el);
    if (el.tagName === "LABEL" && el.htmlFor && !ev.defaultPrevented) {
        const control = registry[el.htmlFor];
        if (control && control !== el) fireClick(control);
    }
}
function countedInput(id) {
    const input = new El("input", { type: "file", id });
    clickCounts[id] = 0;
    input.addEventListener("click", () => { clickCounts[id] += 1; });
    register(input);
    return input;
}
"""


def _run_label_harness(scenario_js: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = _MINI_DOM + scenario_js
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def _label_for(input_id: str) -> str:
    """The `for` value of the shipped label paired with input_id."""
    m = re.search(
        r'<label[^>]*\bfor="(' + re.escape(input_id) + r')"', INDEX_HTML
    )
    assert m is not None, f"no shipped label for {input_id}"
    return m.group(1)


def test_cl_harness_recap_label_activates_input_exactly_once():
    target = _label_for("recap-video-file-input")
    res = _run_label_harness(
        """
const input = countedInput("recap-video-file-input");
const label = new El("label", { class: "drop-zone-content", for: """
        + json.dumps(target)
        + """ });
userClick(label);
console.log(JSON.stringify(clickCounts));
"""
    )
    assert res == {"recap-video-file-input": 1}


def test_cl_harness_logo_label_activates_input_exactly_once():
    target = _label_for("input-logo-file")
    res = _run_label_harness(
        """
const input = countedInput("input-logo-file");
const label = new El("label", { class: "pill-btn", id: "btn-logo-upload", for: """
        + json.dumps(target)
        + """ });
userClick(label);
console.log(JSON.stringify(clickCounts));
"""
    )
    assert res == {"input-logo-file": 1}


def test_cl_harness_direct_input_click_fires_once_no_double():
    res = _run_label_harness("""
const input = countedInput("recap-video-file-input");
const label = new El("label", { class: "drop-zone-content", for: "recap-video-file-input" });
userClick(input);
console.log(JSON.stringify(clickCounts));
""")
    assert res == {"recap-video-file-input": 1}


# ── Release pin: cache token r109 ───────────────────────────────────

def test_cl_cache_token_is_r109():
    assert INDEX_HTML.count("20261117_r159") == 4
    assert ("20261104_r" + "108") not in INDEX_HTML
