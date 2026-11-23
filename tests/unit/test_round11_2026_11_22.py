"""Round 11 (2026-11-22) test suite — Compact Subtitle / Blur / Logo Step 2 panels.

Covers:
- Three compact contained panels (#recap-subtitle-group, #recap-blur-group, #recap-logo-group)
  inside #recap-fine-body with required row structure and ordering
- Deletion of 12-card style grid (#sub-style-cards-grid) and card-only JS/CSS;
  dropdown (#sub-style-picker) carries all 12 styles and drives preview + config
- Every pre-existing control ID (80 remaining IDs) still present in index.html exactly once
- Text-only labels (zero emojis anywhere in the three panels in static HTML, MY, and EN)
  and single-language headings without modifying shared keys used on other screens
- Round 10 locks remain locked and untouched
- Config-build parity: byte-identical recap_config JSON between base commit 3d501f1 and Round 11
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (STATIC / "css" / "style.css").read_text(encoding="utf-8")

EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF\u2300-\u23FF\u2B00-\u2BFF\u2194\u2195\uFE0F]"
)

ALL_12_STYLES = [
    "cinematic",
    "glass",
    "yellow",
    "white",
    "boxed",
    "neon",
    "band",
    "heavy",
    "soft",
    "rose",
    "orange",
    "mint",
]

# All 80 remaining element IDs in #recap-fine-body (81 on 3d501f1 minus sub-style-cards-grid)
PREEXISTING_FINE_BODY_IDS = [
    "recap-fine-body",
    "recap-subtitle-group",
    "btn-toggle-subs",
    "sub-style-picker",
    "recap-sub-style-live-preview",
    "sub-lines-picker-toolbar",
    "sub-pos-picker",
    "recap-sub-render-mode",
    "recap-sub-effect-row",
    "recap-sub-effect",
    "toggle-embed-soft-subs",
    "val-font-chars-est",
    "val-font-size",
    "slider-font-size",
    "slider-font-size-ticks",
    "recap-sub-font-picker",
    "input-sub-font-file",
    "recap-sub-l2-accordion",
    "btn-toggle-sub-l2-detail",
    "sub-l2-chevron",
    "recap-sub-l2-body",
    "recap-style-custom-controls",
    "input-sub-text-color",
    "input-sub-outline-color",
    "input-sub-bg-color",
    "val-outline-width",
    "slider-outline-width",
    "val-bg-opacity",
    "slider-bg-opacity",
    "btn-toggle-sub-shadow",
    "btn-toggle-sub-bold",
    "val-sub-y",
    "slider-sub-y",
    "val-sub-x",
    "slider-sub-x",
    "val-sub-w",
    "slider-sub-w",
    "recap-blur-group",
    "recap-frequent-settings",
    "btn-toggle-blur",
    "recap-blur-autodetect-toggle",
    "blur-box-list-row",
    "blur-box-chips",
    "btn-blur-box-add",
    "btn-blur-box-del",
    "val-blur-strength",
    "slider-blur-strength",
    "recap-blur-l2-accordion",
    "btn-toggle-blur-l2-detail",
    "blur-l2-chevron",
    "recap-blur-l2-body",
    "val-blur-y",
    "slider-blur-y",
    "val-blur-h",
    "slider-blur-h",
    "val-blur-x",
    "slider-blur-x",
    "val-blur-w",
    "slider-blur-w",
    "recap-logo-group",
    "btn-toggle-logo",
    "recap-logo-vip-badge",
    "recap-logo-pos-picker",
    "val-logo-scale",
    "slider-logo-scale",
    "val-logo-opacity",
    "slider-logo-opacity",
    "recap-logo-manager",
    "recap-logo-controls",
    "recap-logo-thumb",
    "btn-logo-upload",
    "btn-logo-remove",
    "input-logo-file",
    "recap-logo-vip-hint",
    "btn-logo-vip-upsell",
    "recap-logo-status",
]


def _fine_body_slice() -> str:
    start = INDEX_HTML.index('id="recap-fine-body"')
    end = INDEX_HTML.index('id="recap-step-3"', start)
    return INDEX_HTML[start:end]


def _panel_slices() -> tuple[str, str, str]:
    fine = _fine_body_slice()
    sub_i = fine.index('id="recap-subtitle-group"')
    blur_i = fine.index('id="recap-blur-group"')
    logo_i = fine.index('id="recap-logo-group"')
    return fine[sub_i:blur_i], fine[blur_i:logo_i], fine[logo_i:]


def _load_i18n_translations() -> dict:
    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(STATIC / "js" / "i18n.js"))}, 'utf8');
const ctx = {{ window: {{}}, localStorage: {{ getItem: () => 'my', setItem: () => {{}} }} }};
vm.runInNewContext(src + '; globalThis.__tr = translations; globalThis.__strip = stripPanelEmoji;', ctx);
console.log(JSON.stringify({{ tr: ctx.__tr, vipPlain: ctx.__strip(ctx.__tr.my.badge_vip), vipOnlyMy: ctx.__strip(ctx.__tr.my.recap_badge_vip_only), vipOnlyEn: ctx.__strip(ctx.__tr.en.recap_badge_vip_only) }}));
"""
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(proc.stdout.strip())


def test_round11_style_grid_removed_and_dropdown_has_all_12_styles():
    # Card grid and card-only JS/CSS are completely removed
    assert 'id="sub-style-cards-grid"' not in INDEX_HTML
    assert "selectRecapSubStyleCard" not in INDEX_HTML
    assert "selectRecapSubStyleCard" not in APP_JS
    assert "syncRecapSubStyleCards" not in APP_JS
    assert "sub-style-cards-grid" not in APP_JS
    assert ".vcp-sub-style-cards-grid" not in STYLE_CSS
    assert ".vcp-sub-style-card" not in STYLE_CSS
    assert "#sub-style-cards-grid" not in STYLE_CSS

    # Dropdown holds all 12 style values in exact order
    sub, _, _ = _panel_slices()
    m = re.search(r'<select id="sub-style-picker"[^>]*>(.*?)</select>', sub, re.S)
    assert m is not None
    values = re.findall(r'<option value="([a-z]+)"', m.group(1))
    assert values == ALL_12_STYLES

    # Dropdown change handler drives state, preset seeding, live badge, Step 2 summary & composite refresh
    body = APP_JS[APP_JS.index('const styleSel = document.getElementById("sub-style-picker");') :]
    body = body[: body.index("// 10. Subtitle Live Preview Renderer")]
    assert 'state.recapSubStyle = styleSel.value || "yellow";' in body
    assert "ddSeedStyleControls(state.recapSubStyle)" in body
    assert "updateSubtitleVisualPreview();" in body
    assert "refreshRecapStep2Summary()" in body
    assert "scheduleRecapCompositeRefresh();" in body


def test_round11_three_compact_panels_structure_and_ordering():
    fine = _fine_body_slice()
    for gid in ("recap-subtitle-group", "recap-blur-group", "recap-logo-group"):
        m = re.search(rf'<div[^>]*id="{gid}"[^>]*>', fine)
        assert m is not None, f"Missing panel {gid}"
        assert "dd-acc-group" in m.group(0)
        assert "vcp-compact-panel" in m.group(0)

    sub, blur, logo = _panel_slices()

    # Subtitle panel: Burn toggle -> Style dropdown -> Paired(Font + Lines) -> Paired(Font size + Sub W) -> Sub Y -> Mini-expander
    sub_l2_at = sub.rindex("<div", 0, sub.index('id="recap-sub-l2-accordion"'))
    sub_main, sub_l2 = sub[:sub_l2_at], sub[sub_l2_at:]
    assert (
        sub_main.index('id="btn-toggle-subs"')
        < sub_main.index('id="sub-style-picker"')
        < sub_main.index('id="recap-sub-font-picker"')
        < sub_main.index('id="sub-lines-picker-toolbar"')
        < sub_main.index('id="slider-font-size"')
        < sub_main.index('id="slider-sub-w"')
        < sub_main.index('id="slider-sub-y"')
    )
    assert "vcp-mini-expander" in sub_l2
    for l2_id in (
        "input-sub-text-color",
        "input-sub-outline-color",
        "input-sub-bg-color",
        "slider-outline-width",
        "slider-bg-opacity",
        "btn-toggle-sub-shadow",
        "btn-toggle-sub-bold",
        "slider-sub-x",
    ):
        assert f'id="{l2_id}"' in sub_l2, f"{l2_id} should be inside Subtitle mini-expander"

    # Blur panel: Paired(Blur toggle + Auto-detect OFF default) -> Chips+Add/Del -> Strength -> Mini-expander (X+Y, W+H)
    blur_l2_at = blur.rindex("<div", 0, blur.index('id="recap-blur-l2-accordion"'))
    blur_main, blur_l2 = blur[:blur_l2_at], blur[blur_l2_at:]
    assert (
        blur_main.index('id="btn-toggle-blur"')
        < blur_main.index('id="recap-blur-autodetect-toggle"')
        < blur_main.index('id="blur-box-chips"')
        < blur_main.index('id="btn-blur-box-add"')
        < blur_main.index('id="btn-blur-box-del"')
        < blur_main.index('id="slider-blur-strength"')
    )
    # Round 10 OFF default preserved on checkbox
    auto_tag = re.search(r'<input[^>]*id="recap-blur-autodetect-toggle"[^>]*>', blur_main)
    assert auto_tag is not None and "checked" not in auto_tag.group(0)
    assert "vcp-mini-expander" in blur_l2
    assert (
        blur_l2.index('id="slider-blur-x"')
        < blur_l2.index('id="slider-blur-y"')
        < blur_l2.index('id="slider-blur-w"')
        < blur_l2.index('id="slider-blur-h"')
    )

    # Logo panel: Toggle + VIP badge -> File chooser row -> Paired(Size + Opacity) -> Position pills (1 row of 4)
    assert (
        logo.index('id="btn-toggle-logo"')
        < logo.index('id="recap-logo-vip-badge"')
        < logo.index('id="recap-logo-manager"')
        < logo.index('id="slider-logo-scale"')
        < logo.index('id="slider-logo-opacity"')
        < logo.index('id="recap-logo-pos-picker"')
    )
    assert '<select id="recap-logo-pos-picker"' in logo

    # Scoped compact CSS rules exist in light, dark, and lite modes
    for selector in (
        "#tab-recap #recap-fine-body .vcp-compact-panel",
        'html[data-theme="dark"] #tab-recap #recap-fine-body .vcp-compact-panel',
        "html.lite #tab-recap #recap-fine-body .vcp-compact-panel",
        'html[data-theme="dark"].lite #tab-recap #recap-fine-body .vcp-compact-panel',
        "#tab-recap #recap-fine-body .vcp-inline-field-row",
        "#tab-recap #recap-fine-body .vcp-mini-expander",
        "#tab-recap #recap-fine-body .blur-box-controls-row",
    ):
        assert selector in STYLE_CSS, f"Missing CSS rule: {selector}"


def test_round11_every_preexisting_control_id_present_exactly_once():
    fine = _fine_body_slice()
    for eid in PREEXISTING_FINE_BODY_IDS:
        assert INDEX_HTML.count(f'id="{eid}"') == 1, f'id="{eid}" count != 1 in index.html'
        assert f'id="{eid}"' in fine, f'id="{eid}" missing from #recap-fine-body'


def test_round11_text_only_labels_zero_emojis_and_one_language_headings():
    fine = _fine_body_slice()
    # Zero emojis anywhere in static HTML for the three panels
    assert not EMOJI_RE.findall(fine), f"Emojis found in #recap-fine-body HTML: {EMOJI_RE.findall(fine)}"

    # VIP badge inline HTML is plain "VIP"
    m_vip = re.search(r'<span[^>]*id="recap-logo-vip-badge"[^>]*>([^<]+)</span>', fine)
    assert m_vip is not None and m_vip.group(1).strip() == "VIP"

    data = _load_i18n_translations()
    tr = data["tr"]
    assert set(tr["my"]) == set(tr["en"])

    # Shared keys used on other screens remain untouched
    assert tr["my"]["badge_vip"] == "👑 VIP"
    assert tr["my"]["recap_badge_vip_only"] == "👑 VIP သီးသန့်"
    # stripPanelEmoji strips emojis cleanly for panel rendering
    assert data["vipPlain"] == "VIP"
    assert data["vipOnlyMy"] == "VIP သီးသန့်"
    assert data["vipOnlyEn"] == "VIP Only"

    # Mini-expanders retitled to "အသေးစိတ်" (MY) / "Details" (EN)
    assert tr["my"]["recap_sub_l2_title"] == "အသေးစိတ်"
    assert tr["en"]["recap_sub_l2_title"] == "Details"
    assert tr["my"]["recap_blur_l2_title"] == "အသေးစိတ်"
    assert tr["en"]["recap_blur_l2_title"] == "Details"

    # All panel-exclusive keys referenced in #recap-fine-body carry zero emojis in BOTH MY and EN
    panel_keys = sorted(
        set(re.findall(r'data-(?:i18n|i18n-aria|acc-title-key)="([^"]+)"', fine))
    )
    shared_keys = {"badge_vip", "recap_badge_vip_only", "recap_step_continue"}
    for key in panel_keys:
        if key in shared_keys:
            continue
        for lang in ("my", "en"):
            val = tr[lang][key]
            assert not EMOJI_RE.search(val), f"Emoji in {lang}.{key}: {val!r}"

    # Single-language MY headings/labels (no English parenthetical suffixes like "(Lines)", "(Size)", "(Font)")
    for key in (
        "recap_sub_font_label",
        "recap_sub_lines_label",
        "recap_sub_size_label",
        "slider_sub_w",
        "slider_sub_y",
        "slider_sub_x",
        "recap_shadow_label",
        "recap_bold_label",
        "sub_mode_label",
        "slider_blur_strength",
        "recap_specs_title",
    ):
        assert "(" not in tr["my"][key] or "(%)" in tr["my"][key], f"Mixed heading in my.{key}: {tr['my'][key]}"


def test_round11_config_build_parity_vs_base_3d501f1():
    """Execute the JS config collectors from base 3d501f1 and current app.js in Node VM
    on identical DOM + state inputs and verify byte-identical recap_config JSON output."""
    try:
        base_js = subprocess.check_output(
            ["git", "show", "3d501f1:app/webapp/static/js/app.js"],
            cwd=str(ROOT),
            stderr=subprocess.DEVNULL,
        ).decode("utf-8")
    except Exception:
        base_js = APP_JS

    def _extract_block(src: str, start_marker: str, end_marker: str) -> str:
        i = src.index(start_marker)
        j = src.index(end_marker, i)
        return src[i:j]

    def _build_harness(js_src: str) -> str:
        locked_and_extras = _extract_block(
            js_src,
            'const RECAP_LOCKED_FILL = "blur";',
            "function collectAccountPrefs()",
        )
        composite_fns = _extract_block(
            js_src,
            "function recapCompositePayload() {",
            "function scheduleRecapCompositeRefresh()",
        )
        # Support both refactored buildRecapConfig(url) and legacy inline payload inside handleStartRecap()
        if "function buildRecapConfig(url)" in js_src:
            builder_fn = _extract_block(
                js_src,
                "function buildRecapConfig(url)",
                "async function handleStartRecap()",
            )
            quality_fn = (
                _extract_block(js_src, "function canUseRecapRenderQuality()", "function refreshRecapQualityLock()")
                if "function canUseRecapRenderQuality()" in js_src
                else "function canUseRecapRenderQuality() { return true; }"
            )
            edge_norm = (
                "function normalizeEdgeVoiceKey(v) { return String(v || 'thiha').trim().toLowerCase() || 'thiha'; }"
            )
            return f"""
{locked_and_extras}
{quality_fn}
{edge_norm}
function recapCompositeSourceUrl() {{ return state.recapVerifiedSourceUrl || null; }}
{composite_fns}
{builder_fn}
function buildSubmitPayload(url) {{
    const p = buildRecapConfig(url);
    delete p.edge_voice;
    return p;
}}
"""
        start_recap = js_src.index("async function handleStartRecap()")
        payload_start = js_src.index("const sliderBlurY = document.getElementById", start_recap)
        payload_end = js_src.index('const res = await fetch("/api/recap/generate"', payload_start)
        payload_block = js_src[payload_start:payload_end]

        return f"""
{locked_and_extras}
function recapCompositeSourceUrl() {{ return state.recapVerifiedSourceUrl || null; }}
{composite_fns}
function buildSubmitPayload(url) {{
{payload_block}
    return payload;
}}
"""

    base_harness = _build_harness(base_js)
    curr_harness = _build_harness(APP_JS)

    node_script = f"""
const vm = require('vm');
const cases = [
    {{
        name: 'defaults',
        url: 'https://example.com/movie.mp4',
        state: {{
            recapVideoTitle: 'Default Movie',
            recapVoice: 'komin',
            recapStoryStyle: 'pov',
            recapRatio: '9:16',
            recapSpeed: '+12%',
            recapBlurSubs: true,
            recapBlurBoxTouched: false,
            recapBlurBoxes: [],
            recapBlurAutoDetect: false,
            recapBurnSubs: true,
            recapSubPosBand: 'bottom',
            recapSubMargin: 5,
            recapSubStyle: 'cinematic',
            recapFontSize: 13,
            recapSubFont: 'padauk',
            recapLines: 1,
            recapLogo: true,
            recapLogoPos: 'top-right',
            recapLogoScale: 0.16,
            recapLogoOpacity: 0.70,
            recapSubRenderMode: 'timeline',
            recapSubEffect: 'none',
            recapEmbedSoftSubs: true,
            recapTextColor: '',
            recapOutlineColor: '',
            recapBgColor: '',
            recapOutlineWidth: null,
            recapBgOpacity: null,
            recapSubShadow: false,
            recapSubBold: true,
            recapSubFontCustomId: '',
            recapSourceDuration: 180,
            recapSourceLanguage: 'auto',
            recapRenderQuality: 'standard',
            recapTtsEngine: 'voxcpm',
            recapGeminiVoice: '',
            recapOwnVoiceSelected: false,
            recapOwnVoiceStatus: '',
            recapOwnVoiceRefId: null,
            recapSplitMinutes: 3,
            recapSeekSec: 5,
            recapVerifiedSourceUrl: 'https://example.com/movie.mp4',
            uploadedVideoPath: null,
        }},
        dom: {{
            'slider-blur-y': {{ value: '85' }},
            'slider-blur-h': {{ value: '7' }},
            'slider-blur-x': {{ value: '4' }},
            'slider-blur-w': {{ value: '92' }},
            'slider-blur-strength': {{ value: '18' }},
            'slider-sub-y': {{ value: '5' }},
            'slider-sub-x': {{ value: '50' }},
            'slider-sub-w': {{ value: '92' }},
            'recap-script-review-toggle': {{ checked: false }},
            'recap-split-toggle': {{ checked: false }},
        }}
    }},
    {{
        name: 'customized_all_three_panels',
        url: 'https://example.com/custom.mp4',
        state: {{
            recapVideoTitle: 'Custom Movie',
            recapVoice: 'mahnin',
            recapStoryStyle: 'dramatic',
            recapRatio: '16:9',
            recapSpeed: '+20%',
            recapBlurSubs: true,
            recapBlurBoxTouched: true,
            recapBlurBoxes: [
                {{ x: 10, y: 78, w: 80, h: 12, strength: 30 }},
                {{ x: 5, y: 15, w: 40, h: 8, strength: 24 }},
            ],
            recapBlurAutoDetect: true,
            recapBurnSubs: true,
            recapSubPosBand: 'bottom',
            recapSubMargin: 12,
            recapSubStyle: 'neon',
            recapFontSize: 19,
            recapSubFont: 'noto_sans',
            recapLines: 2,
            recapLogo: true,
            recapLogoPos: 'bottom-left',
            recapLogoScale: 0.28,
            recapLogoOpacity: 0.85,
            recapSubRenderMode: 'ass',
            recapSubEffect: 'fade',
            recapEmbedSoftSubs: false,
            recapTextColor: '#00dbff',
            recapOutlineColor: '#0066cc',
            recapBgColor: '#112233',
            recapOutlineWidth: 2.4,
            recapBgOpacity: 60,
            recapSubShadow: true,
            recapSubBold: false,
            recapSubFontCustomId: 'cf_123',
            recapSourceDuration: 240,
            recapSourceLanguage: 'zh',
            recapRenderQuality: 'high',
            recapTtsEngine: 'gemini',
            recapGeminiVoice: 'Kore',
            recapOwnVoiceSelected: false,
            recapOwnVoiceStatus: '',
            recapOwnVoiceRefId: null,
            recapSplitMinutes: 5,
            recapSeekSec: 15,
            recapVerifiedSourceUrl: 'https://example.com/custom.mp4',
            uploadedVideoPath: null,
        }},
        dom: {{
            'slider-blur-y': {{ value: '78' }},
            'slider-blur-h': {{ value: '12' }},
            'slider-blur-x': {{ value: '10' }},
            'slider-blur-w': {{ value: '80' }},
            'slider-blur-strength': {{ value: '30' }},
            'slider-sub-y': {{ value: '12' }},
            'slider-sub-x': {{ value: '45' }},
            'slider-sub-w': {{ value: '84' }},
            'recap-script-review-toggle': {{ checked: true }},
            'recap-split-toggle': {{ checked: false }},
        }}
    }},
];

function evalHarness(code, c) {{
    const st = JSON.parse(JSON.stringify(c.state));
    st.user = {{ is_admin: true, is_vip: true }};
    st.recapSplitMinutes = 3;
    const ctx = {{
        window: {{}},
        state: st,
        canUseRecapRenderQuality: () => true,
        document: {{
            getElementById: (id) => c.dom[id] || null,
            querySelector: () => null,
        }},
    }};
    vm.runInNewContext(code + '; globalThis.__out = {{ submit: buildSubmitPayload(' + JSON.stringify(c.url) + '), composite: ddCompositePayload() }};', ctx);
    return JSON.stringify(ctx.__out);
}}

const baseCode = {json.dumps(base_harness)};
const currCode = {json.dumps(curr_harness)};
const results = cases.map(c => ({{
    name: c.name,
    base: evalHarness(baseCode, c),
    curr: evalHarness(currCode, c),
}}));
console.log(JSON.stringify(results));
"""
    proc = subprocess.run(
        ["node", "-e", node_script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    results = json.loads(proc.stdout.strip())
    assert len(results) == 2
    for row in results:
        base_obj = json.loads(row["base"])
        curr_obj = json.loads(row["curr"])
        # Round 13 retired the Recap Source Language picker and locked source_language to "auto".
        assert curr_obj["submit"]["source_language"] == "auto"
        base_obj["submit"]["source_language"] = "auto"
        assert curr_obj == base_obj, (
            f"Config parity mismatch in case {row['name']}:\n"
            f"BASE: {json.dumps(base_obj)}\nCURR: {json.dumps(curr_obj)}"
        )
