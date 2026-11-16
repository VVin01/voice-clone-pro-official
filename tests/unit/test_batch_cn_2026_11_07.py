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
    assert HTML.count("20261116_r159") == 4
    assert "20261104_r109" not in HTML


# ── CN-1: key-card first-glyph clip ─────────────────────────────────

def test_cn1_key_card_keeps_the_shared_card_classes():
    # The fix is NOT a markup change: the key card carries exactly
    # the engine card's horizontal-inset classes (the shared .card
    # rhythm), so the two cards can never drift apart in markup.
    pane = _tts_pane()
    engine = re.search(r'<div class="([^"]+)"[^>]*>\s*<div class="card-label" data-i18n="tts_engine_label"', pane)
    key = re.search(r'<div class="([^"]+)" id="tts-gemini-key-card"', pane)
    assert engine and key
    assert key.group(1) == engine.group(1) == "card glass input-card"


def test_cn1_base_card_padding_is_unchanged():
    card_rule = _between(CSS, ".card {", "}")
    assert "padding: var(--s4);" in card_rule


def test_cn1_key_card_start_inset_rule():
    # One extra spacing step of start padding, scoped to the card:
    # every child (text lines, input row, button row) clears the
    # card's left glass edge uniformly.
    assert "#tts-gemini-key-card { padding-inline-start: calc(var(--s4) + var(--s2)); }" in CSS
    # …and it is the ONLY rule keyed on the card (no per-child hacks).
    assert CSS.count("#tts-gemini-key-card") == 1


# ── CN-2: tap-to-expand detail blocks ───────────────────────────────

OLD_MY_COST_HINT = "💡 မိတ်ဆက်ဆု အချိန်ကို အရင် သုံးပြီး ကျန်တာကို နေ့စဉ် TTS ခွင့်ပြုချက်ထဲက ကောက်ခံပါသည်။ Voice Clone မိနစ်နှင့် Recap Credit လုံးဝ မထိပါ။"
OLD_EN_COST_HINT = "💡 Referral time is spent first; the rest counts against your daily TTS allowance. Voice Clone minutes and Recap Credits are never touched."


def test_cn2_cost_hint_is_one_short_line_with_an_expandable_rest():
    assert _i18n_values("tts_cost_hint") == [
        "💡 မိတ်ဆက်ဆု အချိန်ကို အရင် သုံးပါတယ်",
        "💡 Referral time is spent first",
    ]
    assert _i18n_values("tts_cost_hint_more") == [
        "TTS အသံကို ဒီ Tab ရဲ့ လက်ကျန်ထဲကပဲ ဖြတ်ပါတယ်။ တစ်နေ့ အခမဲ့ 15 မိနစ် (VIP အတွက် 60 မိနစ်) ရပြီး Referral ဆု စက္ကန့်တွေကို အရင် သုံးပေးပါတယ်။ တစ်ခါ ထုတ်ရင် စာလုံး 1,000 (VIP 5,000) အထိ ရပါတယ်။ Gemini အသံ အခမဲ့ မြည်းစမ်းခွင့် တစ်နေ့ 2 မိနစ် ပါဝင်ပြီး ကိုယ်ပိုင် Key ထည့်ထားရင် မြည်းစမ်း ကန့်သတ်ချက် မရှိတော့ပါ။ Voice Clone မိနစ် နဲ့ Recap Credit လုံးဝ မထိပါ။",
        "TTS audio is charged only to this tab's own balance. You get 15 free minutes a day (60 for VIP), and referral reward seconds are spent first. Each generation can be up to 1,000 characters (5,000 for VIP). Gemini voices include a 2-minute free taste per day; if you add your own key, the taste limit no longer applies. Voice Clone minutes and Recap Credits are never touched.",
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
        ("tts-key-guide-toggle", "tts-key-guide-body"),
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
    guide_btn = _between(HTML, 'id="tts-key-guide-toggle"', "</button>")
    assert 'data-i18n="tts_key_guide_title"' in guide_btn
    assert "detail-toggle-chevron" in cost_btn and "detail-toggle-chevron" in guide_btn


def test_cn2_guide_block_lives_inside_the_key_card():
    # So CN-3's Edge-mode card hide takes the whole expander with
    # it — the block can never float visible over a hidden card.
    card = _card_span("tts-gemini-key-card")
    for needle in (
        'id="tts-key-guide-toggle"',
        'id="tts-key-guide-body"',
        'data-i18n="tts_key_guide_1"',
        'data-i18n="tts_key_guide_2"',
        'data-i18n="tts_key_guide_3"',
        'data-i18n="tts_key_honest_1"',
        'data-i18n="tts_key_honest_2"',
    ):
        assert needle in card, needle
    # State line, input and the save/delete buttons stay OUTSIDE
    # the collapsible body (always visible).
    body = _between(card, 'id="tts-key-guide-body"', "</div>")
    for needle in ('id="tts-key-state"', 'id="tts-key-input"', 'id="btn-tts-key-save"'):
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

def test_cn3_edge_voice_hint_element_and_copy():
    assert _i18n_values("tts_edge_voice_hint") == [
        "Edge မှာ မြန်မာ အသံ ၂ မျိုးပဲ ရပါတယ်",
        "Edge offers 2 Myanmar voices",
    ]
    pane = _tts_pane()
    hint = '<p class="ref-desc hidden" id="tts-edge-voice-hint" data-i18n="tts_edge_voice_hint">Edge မှာ မြန်မာ အသံ ၂ မျိုးပဲ ရပါတယ်</p>'
    assert hint in pane
    # It sits with the Edge voice row it explains (before the
    # balance line), inside the engine card.
    assert pane.index('id="tts-edge-voice-row"') < pane.index('id="tts-edge-voice-hint"') < pane.index('id="tts-balance-line"')


def test_cn3_visibility_painter_covers_all_surfaces():
    fn = _between(APP_JS, "function applyTtsEngineVisibility", "function initTtsTab")
    for el_id in (
        "tts-gemini-voice-row",
        "tts-edge-voice-row",
        "tts-gemini-key-card",
        "tts-edge-voice-hint",
        "tts-taste-note",
    ):
        assert f'getElementById("{el_id}")' in fn, el_id
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
    # _cgPromptAddKey scrolls to the key card; it can only be
    # reached from the Item 3b prompt, which generateTTS shows
    # exclusively on a Gemini generate — so it can never fire
    # while Edge mode has the card hidden.
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
const paintSrc = extract("paintTtsTasteNote", "async function saveTtsGeminiKey");
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

const ids = ["tts-gemini-voice-row", "tts-edge-voice-row", "tts-gemini-key-card",
             "tts-edge-voice-hint", "tts-taste-note", "tts-cost-hint-more", "tts-key-guide-body"];
const els = {};
ids.forEach(id => { els[id] = new El(id); });
["tts-edge-voice-row", "tts-edge-voice-hint", "tts-taste-note", "tts-cost-hint-more", "tts-key-guide-body"]
    .forEach(id => els[id].classList.add("hidden"));

const document = { getElementById: id => els[id] || null };
const ttsState = { engine: "gemini", speed: "+12%", jobId: null, pollTimer: null };
let _cgQuotaCache = null;
const t = k => k;
const tf = (k, args) => k + ":" + JSON.stringify(args);

eval(paintSrc + "\n" + toggleSrc + "\n" + visSrc);

const H = id => els[id].classList.contains("hidden");
const results = {};
// Init state (gemini, no quota yet): key card + gemini row shown,
// edge row/hint hidden, taste note hidden until a quota paints it.
applyTtsEngineVisibility();
results.initial = { keyCard: H("tts-gemini-key-card"), gRow: H("tts-gemini-voice-row"),
                    eRow: H("tts-edge-voice-row"), hint: H("tts-edge-voice-hint"), taste: H("tts-taste-note") };
// Edge: the whole Gemini surface (key card + taste note) hides,
// the hint explains the short voice list.
ttsState.engine = "edge";
applyTtsEngineVisibility();
results.edge = { keyCard: H("tts-gemini-key-card"), taste: H("tts-taste-note"), hint: H("tts-edge-voice-hint"),
                 gRow: H("tts-gemini-voice-row"), eRow: H("tts-edge-voice-row") };
// Back to Gemini with taste remaining: the note returns via the
// quota painter, not a bare unhide.
ttsState.engine = "gemini";
_cgQuotaCache = { gemini_taste_remaining_seconds: 120, has_own_key: false };
applyTtsEngineVisibility();
results.gemini = { keyCard: H("tts-gemini-key-card"), hint: H("tts-edge-voice-hint"), taste: H("tts-taste-note"),
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
const b2 = fakeBtn("tts-key-guide-body");
toggleTtsDetail(b2);
results.guideOpen = { body: H("tts-key-guide-body"), aria: b2.getAttribute("aria-expanded") };
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
    assert r["initial"] == {"keyCard": False, "gRow": False, "eRow": True, "hint": True, "taste": True}
    assert r["edge"] == {"keyCard": True, "taste": True, "hint": False, "gRow": True, "eRow": False}
    assert r["gemini"]["keyCard"] is False and r["gemini"]["hint"] is True
    assert r["gemini"]["taste"] is False and "tts_taste_remaining" in r["gemini"]["tasteText"]
    assert r["costOpen"] == {"body": False, "aria": "true", "open": True}
    assert r["costClosed"] == {"body": True, "aria": "false"}
    assert r["guideOpen"] == {"body": False, "aria": "true"}
