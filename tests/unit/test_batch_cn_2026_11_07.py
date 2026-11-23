"""Batch CN — TTS tab fixes (2026-11-07).

Owner direction (2026-11-07 ~23:41 "tts tab ကိုပြင်ပေး မျက်မြင်ကြည့်ပီး",
~23:56 "စာတန်းတွေအရှည်ကြီးဖြစ်နေတာ" — the tab's text lines run too
long), after a two-pass eyes-on on Milo (FREE, fresh) in Telegram
Web A:

- CN-1  The Gemini key card's lines lost their first glyphs at
        the card's left edge (~10–15px), verified card-intrinsic:
        clipped in the upper AND lower screen thirds, unchanged by
        focus/blur, while the same-width engine card in the same
        screen band rendered clean. Diagnosis (build report): under
        headless-Chromium CDP measurement the card's box geometry
        is pixel-identical to the clean engine card (content-left
        33px both, no transform / negative margin / overflow,
        scrollWidth == clientWidth on the card and every child),
        and no CSS rule or JS distinguishes the card — the loss is
        glyph-ink level, the Myanmar first-glyph clip this codebase
        documented in Batch BB ("their first glyph … was sliced
        flush at the card edge", fixed with the standard inset).
        The key card is a stack of long body lines whose first
        glyphs (Myanmar digits ၁/၂/၃, clusters, Latin "Key") start
        flush at the content edge. Fix: the card gains ONE extra
        spacing step of start padding so every child's content —
        text lines, input row, button row — clears the card's
        hard glass edge uniformly; the shared .card rhythm and
        the card's markup classes are untouched.
- CN-2  The tab's long explanations become tap-to-expand
        (owner amendment: "user သိချင်မှ ကြည့်ရတာ" — visible in
        full only when the user taps). The cost hint keeps ONE
        short line visible (tts_cost_hint shortened); the rest
        (tts_cost_hint_more) unfolds in place. The key card's
        guide block (guide title + guide_1..3 + honest_1..2) is
        ONE expandable block toggled by its title row; the state
        line, input and save/delete stay always visible. ONE
        reusable pattern: a .detail-toggle button (label span +
        chevron, .open rotates the chevron, aria-expanded flips)
        + a .hidden container, wired by the generic
        toggleTtsDetail(btn) via data-detail-target — the
        toggleHomeTutorials idiom. State does not persist.
- CN-3  Edge-mode declutter + voice hint: the Gemini-only
        surfaces (#tts-taste-note, #tts-gemini-key-card) show ONLY
        in Gemini mode, and a new hint (#tts-edge-voice-hint,
        tts_edge_voice_hint) explains the voice list's 10 → 2
        drop in Edge mode (data is correct: Edge has exactly 2
        Myanmar voices; generateTTS reads the engine-appropriate
        select — no stale-selection bug). One painter,
        applyTtsEngineVisibility(), serves the toggle handler
        AND the init paint; in Gemini mode it re-runs
        paintTtsTasteNote on the cached quota so the taste note's
        own quota rules still decide. The CG Item 3b prompt can
        only fire from a Gemini generate (generateTTS gates it on
        ttsState.engine === "gemini"), so _cgPromptAddKey can
        never scroll to a card hidden by Edge mode — pinned below.
- CN-4  Cache token r109 → r110 (the suite-wide release pins
        were swept with it, per the standing convention).

Deliberately unchanged (spec out-of-scope): the TTS box's
pre-filled starter sample, the balance-line wording/values, and
all backend/TTS-service behaviour.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")


def _between(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


def _i18n_values(key: str) -> list:
    """Both locale values for a key, in file order (my, en)."""
    return re.findall(rf'{re.escape(key)}:\s*"([^"]*)"', I18N_JS)


def _tts_pane() -> str:
    return _between(HTML, '<section id="tab-tts"', "</section>")


def _card_span(card_id: str) -> str:
    """The card element's markup, from its opening tag to the next
    sibling card / pane end — enough to prove containment."""
    i = HTML.index(f'id="{card_id}"')
    nxt = HTML.find('<div class="card ', i + 10)
    return HTML[i:nxt if nxt != -1 else len(HTML)]


# ── CN-4: cache token ───────────────────────────────────────────────

def test_cn_cache_token_is_r110():
    # Superseded by Batch CO (r111); CN's own bump landed as r110.
    assert HTML.count("20261123_round15") == 5
    assert "20261104_r109" not in HTML


# ── CN-1: key-card first-glyph clip ─────────────────────────────────

def test_cn1_key_card_moved_to_profile_and_tts_card_retired():
    # Round 8 B7: Gemini Key BYOK card lives in Profile only (removed from TTS tab)
    pane = _tts_pane()
    engine = re.search(
        r'<div class="([^"]+)"[^>]*>\s*(?:<div class="step-badge-title[^"]*">[\s\S]*?)?data-i18n="tts_engine_label"',
        pane,
    )
    assert engine and engine.group(1) == "card glass input-card"
    assert 'id="tts-gemini-key-card"' not in HTML
    assert 'id="profile-gemini-key-card"' in HTML


def test_cn1_base_card_padding_is_unchanged():
    card_rule = _between(CSS, ".card {", "}")
    assert "padding: var(--s4);" in card_rule


def test_cn1_key_card_start_inset_rule_retired_with_tts_card():
    assert CSS.count("#tts-gemini-key-card") == 0


# ── CN-2: tap-to-expand detail blocks ───────────────────────────────

OLD_MY_COST_HINT = "💡 မိတ်ဆက်ဆု အချိန်ကို အရင် သုံးပြီး ကျန်တာကို နေ့စဉ် TTS ခွင့်ပြုချက်ထဲက ကောက်ခံပါသည်။ Voice Clone မိနစ်နှင့် Recap Credit လုံးဝ မထိပါ။"
OLD_EN_COST_HINT = "💡 Referral time is spent first; the rest counts against your daily TTS allowance. Voice Clone minutes and Recap Credits are never touched."


def test_cn2_cost_hint_is_one_short_line_with_an_expandable_rest():
    assert _i18n_values("tts_cost_hint") == [
        "💡 မိတ်ဆက်ဆု အချိန်ကို အရင် သုံးပါတယ်",
        "💡 Referral time is spent first",
    ]
    # Limits round (2026-11-23): per-gen chars 3,000 / 15,000; Gemini pool taste Free 2m / VIP 10m.
    assert _i18n_values("tts_cost_hint_more") == [
        "တစ်နေ့ အခမဲ့ 15 မိနစ် (VIP 60 မိနစ်) · တစ်ကြိမ် စာလုံး 3,000 (VIP 15,000) အထိ ရပါတယ်။ Gemini Pool စမ်းသုံးခွင့် — Free 2 မိနစ်/နေ့ · VIP 10 မိနစ်/နေ့ (ကိုယ်ပိုင် Key ထည့်ထားရင် အကန့်အသတ်မရှိ)။",
        "Daily free TTS: 15 min (VIP 60 min) · Up to 3,000 characters per generation (VIP 15,000). Gemini pool taste: Free 2 min/day · VIP 10 min/day (unlimited with your own key).",
    ]
    # The old wall of text is gone from BOTH surfaces.
    assert OLD_MY_COST_HINT not in I18N_JS and OLD_MY_COST_HINT not in HTML
    assert OLD_EN_COST_HINT not in I18N_JS
    # Inline defaults match the my strings; the more-block starts
    # collapsed and carries the expandable explanation.
    pane = _tts_pane()
    assert '<p class="ref-desc" data-i18n="tts_cost_hint">💡 မိတ်ဆက်ဆု အချိန်ကို အရင် သုံးပါတယ်</p>' in pane
    more = _between(pane, 'id="tts-cost-hint-more"', "</div>")
    assert 'class="hidden" id="tts-cost-hint-more"' in pane
    assert 'data-i18n="tts_cost_hint_more"' in more


def test_cn2_both_toggles_exist_and_start_collapsed():
    assert _i18n_values("tts_details_toggle") == ["အသေးစိတ် ကြည့်ရန်", "View details"]
    for toggle_id, target in (
        ("tts-cost-hint-toggle", "tts-cost-hint-more"),
        ("profile-key-guide-toggle", "profile-key-guide-body"),
    ):
        tag = re.search(rf'<button[^>]*id="{toggle_id}"[^>]*>', HTML)
        assert tag, toggle_id
        assert 'class="detail-toggle' in tag.group(0)
        assert 'aria-expanded="false"' in tag.group(0)
        assert f'data-detail-target="{target}"' in tag.group(0)
        assert 'onclick="toggleTtsDetail(this)"' in tag.group(0)
        assert f'class="hidden" id="{target}"' in HTML
    # The cost toggle carries the shared label key; the guide toggle
    # carries the guide's own title (the title row IS the toggle).
    cost_btn = _between(HTML, 'id="tts-cost-hint-toggle"', "</button>")
    assert 'data-i18n="tts_details_toggle"' in cost_btn
    guide_btn = _between(HTML, 'id="profile-key-guide-toggle"', "</button>")
    assert 'data-i18n="tts_key_guide_title"' in guide_btn
    assert "detail-toggle-chevron" in cost_btn and "detail-toggle-chevron" in guide_btn


def test_cn2_guide_block_lives_inside_the_profile_key_card():
    card = _card_span("profile-gemini-key-card")
    for needle in (
        'id="profile-key-guide-toggle"',
        'id="profile-key-guide-body"',
        'data-i18n="tts_key_guide_1"',
        'data-i18n="tts_key_guide_2"',
        'data-i18n="tts_key_guide_3"',
        'data-i18n="tts_key_honest_1"',
        'data-i18n="tts_key_honest_2"',
    ):
        assert needle in card, needle
    # State line, input and the save/delete buttons stay OUTSIDE
    # the collapsible body (always visible).
    body = _between(card, 'id="profile-key-guide-body"', "</div>")
    for needle in ('id="profile-key-state"', 'id="profile-key-input"', 'id="btn-profile-key-save"'):
        assert needle in card and needle not in body, needle


def test_cn2_toggle_pattern_css_and_js():
    # The toggle row + rotating chevron (toggleHomeTutorials idiom).
    assert ".detail-toggle {" in CSS
    assert ".detail-toggle.open .detail-toggle-chevron { transform: rotate(180deg); }" in CSS
    fn = _between(APP_JS, "function toggleTtsDetail", "window.toggleTtsDetail")
    assert 'getAttribute("data-detail-target")' in fn
    assert 'classList.toggle("hidden")' in fn
    assert 'classList.toggle("open", open)' in fn
    assert 'setAttribute("aria-expanded"' in fn
    assert "window.toggleTtsDetail = toggleTtsDetail;" in APP_JS


# ── CN-3: Edge-mode declutter + voice hint ──────────────────────────

def test_cn3_edge_voice_hint_retired_in_round8_1():
    # Round 8.1 Item 1: #tts-edge-voice-hint and tts_edge_voice_hint removed
    assert 'id="tts-edge-voice-hint"' not in HTML
    assert "tts_edge_voice_hint" not in I18N_JS


def test_cn3_visibility_painter_covers_all_surfaces():
    fn = _between(APP_JS, "function applyTtsEngineVisibility", "function initTtsTab")
    for el_id in (
        "tts-gemini-voice-row",
        "tts-edge-voice-row",
        "tts-taste-note",
    ):
        assert f'getElementById("{el_id}")' in fn, el_id
    assert "tts-edge-voice-hint" not in fn
    # Gemini-only surfaces keyed on the engine, both directions.
    assert 'classList.toggle("hidden", engine !== "gemini")' in fn
    assert 'classList.toggle("hidden", engine !== "edge")' in fn
    # In Gemini mode the taste note's own quota rules decide (the
    # painter defers to paintTtsTasteNote on the cached quota).
    assert "paintTtsTasteNote(_cgQuotaCache)" in fn
    # The toggle handler delegates to the painter, and init paints
    # once so the tab opens consistent with the selected engine.
    init = _between(APP_JS, "function initTtsTab", "document.addEventListener")
    assert init.count("applyTtsEngineVisibility();") >= 2


def test_cn3_item3b_prompt_is_gemini_gated():
    # Round 8 B7: _cgPromptAddKey opens Profile and scrolls to profile-gemini-key-card.
    gen = _between(APP_JS, "async function generateTTS", "window.generateTTS")
    assert 'if (ttsState.engine === "gemini" && await _cgTasteBlocked())' in gen


# ── Behaviour: the real toggle + painter sources under node ─────────

_NODE_BEHAVIOUR = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
function extract(name, endMarker) {
    const i = src.indexOf("function " + name);
    if (i < 0) throw new Error("missing function " + name);
    return src.slice(i, src.indexOf(endMarker, i));
}
const paintSrc = extract("paintTtsTasteNote", "function _applyGeminiKeyMutationView");
const toggleSrc = extract("toggleTtsDetail", "window.toggleTtsDetail");
const visSrc = extract("applyTtsEngineVisibility", "function initTtsTab");

class El {
    constructor(id) {
        this.id = id; this.textContent = ""; this.attrs = {};
        const s = new Set();
        this.classList = {
            add: (...c) => c.forEach(x => s.add(x)),
            remove: (...c) => c.forEach(x => s.delete(x)),
            // DOM contract: returns whether the class is present
            // AFTER the call; a force argument pins the outcome.
            toggle: (c, force) => {
                const want = force === undefined ? !s.has(c) : !!force;
                if (want) s.add(c); else s.delete(c);
                return want;
            },
            contains: c => s.has(c),
        };
    }
    getAttribute(k) { return this.attrs[k] ?? null; }
    setAttribute(k, v) { this.attrs[k] = String(v); }
}

const ids = ["tts-gemini-voice-row", "tts-edge-voice-row",
             "tts-taste-note", "tts-cost-hint-more", "profile-key-guide-body"];
const els = {};
ids.forEach(id => { els[id] = new El(id); });
["tts-edge-voice-row", "tts-taste-note", "tts-cost-hint-more", "profile-key-guide-body"]
    .forEach(id => els[id].classList.add("hidden"));

const window = {};
const document = { getElementById: id => els[id] || null };
const ttsState = { engine: "gemini", speed: "+12%", jobId: null, pollTimer: null };
let _cgQuotaCache = null;
const t = k => k;
const tf = (k, args) => k + ":" + JSON.stringify(args);

eval(paintSrc + "\n" + toggleSrc + "\n" + visSrc);

const H = id => els[id].classList.contains("hidden");
const results = {};
applyTtsEngineVisibility();
results.initial = { gRow: H("tts-gemini-voice-row"),
                    eRow: H("tts-edge-voice-row"), taste: H("tts-taste-note") };
ttsState.engine = "edge";
applyTtsEngineVisibility();
results.edge = { taste: H("tts-taste-note"),
                 gRow: H("tts-gemini-voice-row"), eRow: H("tts-edge-voice-row") };
ttsState.engine = "gemini";
_cgQuotaCache = { gemini_taste_remaining_seconds: 120, has_own_key: false };
applyTtsEngineVisibility();
results.gemini = { taste: H("tts-taste-note"),
                   tasteText: els["tts-taste-note"].textContent };
// Expanders: open → close round trip on both blocks.
function fakeBtn(target) {
    const b = new El("btn");
    b.setAttribute("data-detail-target", target);
    b.setAttribute("aria-expanded", "false");
    return b;
}
const b1 = fakeBtn("tts-cost-hint-more");
toggleTtsDetail(b1);
results.costOpen = { body: H("tts-cost-hint-more"), aria: b1.getAttribute("aria-expanded"), open: b1.classList.contains("open") };
toggleTtsDetail(b1);
results.costClosed = { body: H("tts-cost-hint-more"), aria: b1.getAttribute("aria-expanded") };
const b2 = fakeBtn("profile-key-guide-body");
toggleTtsDetail(b2);
results.guideOpen = { body: H("profile-key-guide-body"), aria: b2.getAttribute("aria-expanded") };
console.log(JSON.stringify(results));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_cn_behaviour_under_node():
    script = Path("/tmp") / "cn_behaviour.js"
    script.write_text(_NODE_BEHAVIOUR, encoding="utf-8")
    out = subprocess.run(
        ["node", str(script), str(ROOT / "app/webapp/static/js/app.js")],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["initial"] == {"gRow": False, "eRow": True, "taste": True}
    assert r["edge"] == {"taste": True, "gRow": True, "eRow": False}
    assert r["gemini"]["taste"] is False and "tts_taste_remaining" in r["gemini"]["tasteText"]
    assert r["costOpen"] == {"body": False, "aria": "true", "open": True}
    assert r["costClosed"] == {"body": True, "aria": "false"}
    assert r["guideOpen"] == {"body": False, "aria": "true"}
