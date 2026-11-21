"""Round 9 tests (2026-11-22): Recap Step 1 cleanup + TTS voice bar."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
I18N_JS = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")


def _translations() -> dict:
    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(STATIC / "js" / "i18n.js"))}, 'utf8');
const ctx = {{ window: {{}}, localStorage: {{ getItem: () => 'my', setItem: () => {{}} }} }};
vm.runInNewContext(src + '; globalThis.__tr = translations;', ctx);
console.log(JSON.stringify(ctx.__tr));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(proc.stdout.strip())


def _tab_slice(tab_id: str, next_tab_id: str) -> str:
    start = INDEX_HTML.index(f'id="{tab_id}"')
    end = INDEX_HTML.index(f'id="{next_tab_id}"', start)
    return INDEX_HTML[start:end]


def test_round9_a1_recap_header_removed():
    tr = _translations()
    recap_tab = _tab_slice("tab-recap", "tab-downloader")

    assert 'class="pane-header"' not in recap_tab
    assert 'data-i18n="recap_title"' not in INDEX_HTML
    assert 'data-i18n="recap_subtitle"' not in INDEX_HTML
    assert "recap_title" not in tr["my"] and "recap_title" not in tr["en"]
    assert "recap_subtitle" not in tr["my"] and "recap_subtitle" not in tr["en"]
    assert re.search(r"\brecap_title\s*:", I18N_JS) is None
    assert re.search(r"\brecap_subtitle\s*:", I18N_JS) is None
    # Hidden demo card and steps nav stay intact
    assert 'id="recap-demo-card"' in recap_tab
    assert 'id="recap-steps-nav"' in recap_tab


def test_round9_a2_help_paragraphs_moved_to_notes_expander():
    recap_tab = _tab_slice("tab-recap", "tab-downloader")
    panel_url = recap_tab[
        recap_tab.index('id="panel-source-url"') : recap_tab.index('id="panel-source-file"')
    ]
    assert 'data-i18n="link_help_recap"' not in panel_url
    assert 'data-i18n="recap_platforms_note"' not in panel_url
    assert 'link-source-help' not in panel_url

    notes_block = recap_tab[
        recap_tab.index('class="recap-notes-collapse') : recap_tab.index("</details>", recap_tab.index('class="recap-notes-collapse'))
    ]
    rights_idx = notes_block.index('data-i18n="recap_rights_note"')
    platforms_idx = notes_block.index('data-i18n="recap_platforms_note"')
    vidmate_idx = notes_block.index('data-i18n="link_help_recap"')
    carousel_idx = notes_block.index('id="studio-alert-carousel"')
    assert rights_idx < platforms_idx < vidmate_idx < carousel_idx
    assert 'class="rights-note recap-rights-note" data-i18n="recap_platforms_note"' in notes_block
    assert 'class="rights-note recap-rights-note" data-i18n="link_help_recap"' in notes_block

    # Untouched Step 1 pieces remain intact
    assert 'id="recap-free-terms-note"' in recap_tab
    assert 'id="recap-video-info-box"' in recap_tab


def test_round9_a3_single_language_toggle_labels_and_dropzone_restyle():
    tr = _translations()
    assert tr["my"]["recap_source_tab_url"] == "🔗 ဗီဒီယို လင့်ခ်"
    assert tr["en"]["recap_source_tab_url"] == "🔗 Video Link (URL)"
    assert tr["my"]["recap_source_tab_file"] == "📁 ဗီဒီယိုဖိုင် တင်ရန်"
    assert tr["en"]["recap_source_tab_file"] == "📁 Upload Video (MP4)"

    assert 'data-i18n="recap_source_tab_url">🔗 ဗီဒီယို လင့်ခ်<' in INDEX_HTML
    assert 'data-i18n="recap_source_tab_file">📁 ဗီဒီယိုဖိုင် တင်ရန်<' in INDEX_HTML

    assert "#tab-recap #recap-step-1 .source-tab-btn" in STYLE_CSS
    assert "white-space: nowrap;" in STYLE_CSS.split("#tab-recap #recap-step-1 .source-tab-btn {", 1)[1].split("}", 1)[0]

    dz_rule = STYLE_CSS.split("#tab-recap #recap-video-drop-zone {", 1)[1].split("}", 1)[0]
    assert "border-style: solid;" in dz_rule
    assert "var(--vcp-flat-glass-bg" in dz_rule
    assert "#tab-recap #recap-video-drop-zone .drop-zone-content" in STYLE_CSS
    assert "#tab-recap #recap-video-drop-zone .drop-text" in STYLE_CSS
    # All file-input and upload progress IDs preserved
    for preserved_id in (
        "recap-video-drop-zone",
        "recap-video-file-input",
        "recap-drop-text",
        "recap-drop-desktop-hint",
        "recap-upload-progress",
        "recap-upload-cancel",
        "recap-upload-error",
        "recap-upload-retry",
    ):
        assert f'id="{preserved_id}"' in INDEX_HTML


def test_round9_a4_recap_bottom_compacted():
    # #tab-recap.active moved out of 148px block into padding-bottom: 0 !important
    b148 = STYLE_CSS.split("#tab-admin.active { padding-bottom:", 1)[0].rsplit(".content-viewport,", 1)[1]
    assert "#tab-recap.active" not in b148

    b0 = STYLE_CSS.split("#tab-home.active,", 1)[1].split("}", 1)[0]
    assert "#tab-recap.active" in b0
    assert "padding-bottom: 0 !important" in b0

    vp_block = STYLE_CSS.split(".content-viewport:has(#tab-home.active),", 1)[1].split("}", 1)[0]
    assert ".content-viewport:has(#tab-recap.active)" in vp_block
    assert "calc(var(--nav-h, 64px) + 32px + env(safe-area-inset-bottom, 0px)) !important" in vp_block


def test_round9_b1_tts_voice_rows_bar_and_no_label():
    tr = _translations()
    tts_tab = _tab_slice("tab-tts", "tab-recap")

    assert "tts_voice_label" not in tts_tab
    assert "tts_voice_label" not in INDEX_HTML
    assert "tts_voice_label" not in tr["my"] and "tts_voice_label" not in tr["en"]
    assert re.search(r"\btts_voice_label\s*:", I18N_JS) is None

    for row_id in ("tts-gemini-voice-row", "tts-edge-voice-row"):
        m = re.search(rf'<div[^>]*id="{row_id}"[^>]*>', tts_tab)
        assert m is not None, f"Missing {row_id}"
        assert "voice-selected-row" in m.group(0)

    # Preview buttons & selects still live inside their respective rows
    gem_row = tts_tab[tts_tab.index('id="tts-gemini-voice-row"') : tts_tab.index('id="tts-edge-voice-row"')]
    assert 'id="tts-gemini-voice-select"' in gem_row
    assert 'id="btn-tts-gemini-preview"' in gem_row
    assert 'class="ctrl-title"' not in gem_row

    edge_row = tts_tab[tts_tab.index('id="tts-edge-voice-row"') : tts_tab.index('id="tts-cost-hint-toggle"')]
    assert 'id="tts-edge-voice-select"' in edge_row
    assert 'id="btn-tts-edge-preview"' in edge_row
    assert 'class="ctrl-title"' not in edge_row

    # Scoped styling tokens for #tab-tts .voice-selected-row (light + dark + lite)
    assert "#tab-tts .voice-selected-row {" in STYLE_CSS
    assert 'html[data-theme="dark"] #tab-tts .voice-selected-row {' in STYLE_CSS
    assert "html.lite #tab-tts .voice-selected-row {" in STYLE_CSS
    assert 'html[data-theme="dark"].lite #tab-tts .voice-selected-row {' in STYLE_CSS
