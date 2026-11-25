"""Wizard step gating & Voice Clone custom stepper truth (owner decision 2026-11-15).

Covers:
1. Recap wizard gated navigation (supersedes AX-1 free navigation):
   - Step n is reachable iff every step < n is complete in RECAP_STEP_DONE.
   - Completed steps and the first-incomplete step stay freely revisitable.
   - Gate is enforced inside showRecapStep() (the one door) so nav chips,
     data-step-next buttons, and programmatic calls all obey it.
   - Tapping a locked step does NOT navigate and surfaces recap_step_locked_hint
     naming the first incomplete step (my + en).
   - Batch CI Step-1 "free-blocked" over-cap check runs before step-order gating.
2. Voice Clone custom stepper truth (#custom-clone-stepper):
   - 1–5 chips remain non-interactive <span> indicators (no buttons/click handlers).
   - CSS renders done (✓ tinted), active (highlighted), and future/locked (muted,
     dashed, lock affordance) across states and light/dark themes, surviving i18n
     innerText refreshes.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.webapp import api as api_mod


ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(api_mod.__file__).resolve().parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (STATIC_DIR / "css" / "style.css").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC_DIR / "js" / "i18n.js").read_text(encoding="utf-8")


def _run_node(script: str, tmp_path: Path) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    tmp = tmp_path / "_wizard_gating_tmp.js"
    try:
        tmp.write_text(script, encoding="utf-8")
        res = subprocess.run([node, str(tmp)], capture_output=True, text=True, timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def _extract_wizard_slice() -> str:
    start = APP_JS.index("const RECAP_STEP_DONE =")
    sync_pos = APP_JS.index("function syncRecapStepsWithSource(", start)
    end = APP_JS.index("\n}\n", sync_pos) + 3
    part1 = APP_JS[start:end]
    init_start = APP_JS.index("function initRecapSteps()")
    init_end = APP_JS.index("window.initRecapSteps = initRecapSteps;", init_start)
    part2 = APP_JS[init_start:init_end]
    clone_start = APP_JS.index("function setCustomCloneStep(")
    clone_end = APP_JS.index("window.setCustomCloneStep = setCustomCloneStep;", clone_start)
    part3 = APP_JS[clone_start:clone_end]
    return "\n".join([part1, part2, part3])


_NODE_DOM_HARNESS = r"""
const window = globalThis;
window.scrollTo = () => {};
function requestAnimationFrame(fn) { try { fn(); } catch (_) {} }
function makeEl(tag, attrs = {}, classes = []) {
    const el = {
        tagName: tag.toUpperCase(),
        textContent: "",
        innerText: "",
        _attrs: { ...attrs },
        _classes: new Set(classes),
        _listeners: {},
        style: {},
        scrollIntoView: () => {},
        focus: () => {},
    };
    el.classList = {
        add: (...cs) => cs.forEach(c => el._classes.add(c)),
        remove: (...cs) => cs.forEach(c => el._classes.delete(c)),
        toggle: (c, force) => {
            if (force === undefined) {
                if (el._classes.has(c)) el._classes.delete(c);
                else el._classes.add(c);
            } else if (force) el._classes.add(c);
            else el._classes.delete(c);
        },
        contains: (c) => el._classes.has(c),
    };
    el.setAttribute = (k, v) => { el._attrs[k] = String(v); };
    el.getAttribute = (k) => (k in el._attrs ? el._attrs[k] : null);
    el.removeAttribute = (k) => { delete el._attrs[k]; };
    el.hasAttribute = (k) => k in el._attrs;
    el.addEventListener = (ev, fn) => { (el._listeners[ev] ||= []).push(fn); };
    el.click = () => { (el._listeners.click || []).forEach(fn => fn({ currentTarget: el, target: el })); };
    return el;
}

const navItems = [1, 2, 3, 4].map(n =>
    makeEl("button", { "data-step-toggle": String(n) },
        n === 1 ? ["recap-steps-nav-item", "is-active"] : ["recap-steps-nav-item", "is-locked"])
);
navItems.slice(1).forEach(b => b.setAttribute("aria-disabled", "true"));
const pills = [1, 2, 3, 4].map(n => makeEl("span", { "data-step-pill": String(n) }));
const nextBtns = [
    makeEl("button", { "data-step-next": "3" }),
    makeEl("button", { "data-step-next": "4" }),
];
const stepCards = {
    1: makeEl("div", { id: "recap-step-1" }, []),
    2: makeEl("div", { id: "recap-step-2" }, ["hidden"]),
    3: makeEl("div", { id: "recap-step-3" }, ["hidden"]),
    4: makeEl("div", { id: "recap-step-4" }, ["hidden"]),
};
const cloneSpans = [1, 2, 3, 4, 5].map(n =>
    makeEl("span", { "data-clone-step": String(n) }, n === 1 ? ["vcp-clone-step", "is-active"] : ["vcp-clone-step", "is-locked"])
);
const cloneStepper = makeEl("div", { id: "custom-clone-stepper" });
cloneStepper.querySelectorAll = (sel) => (sel === "[data-clone-step]" ? cloneSpans : []);
const checkStatus = makeEl("div", { id: "custom-clone-check-status" }, ["hidden"]);

const BY_ID = {
    "recap-steps-nav": makeEl("div", { id: "recap-steps-nav" }),
    "recap-step-1": stepCards[1],
    "recap-step-2": stepCards[2],
    "recap-step-3": stepCards[3],
    "recap-step-4": stepCards[4],
    "tab-recap": makeEl("div", { id: "tab-recap" }),
    "custom-clone-stepper": cloneStepper,
    "custom-clone-check-status": checkStatus,
};
const document = {
    getElementById: (id) => BY_ID[id] || null,
    querySelector: (sel) => {
        const m = sel.match(/\[data-step-toggle="(\d)"\]/);
        if (m) return navItems[parseInt(m[1], 10) - 1] || null;
        return null;
    },
    querySelectorAll: (sel) => {
        if (sel === "#recap-steps-nav [data-step-toggle]" || sel === "[data-step-toggle]") return navItems;
        if (sel === "[data-step-next]") return nextBtns;
        for (const n of [1, 2, 3, 4]) {
            if (sel === `[data-step-pill="${n}"]` || sel === '[data-step-pill="' + n + '"]') return [pills[n - 1]];
        }
        return [];
    },
};

let LANG = "en";
const I18N_MAP = {
    en: {
        recap_step_locked_hint: "Finish step {n} first",
        tier_free_label: "Free",
        toast_recap_over_hint_free: "FREE_LIMIT_HINT",
        toast_recap_over_limit: "OVER_LIMIT|{tier}|{m}|{hint}",
        clone_check_ok: "OK {kb} KB",
    },
    my: {
        recap_step_locked_hint: "\u1021\u1006\u1004\u1037\u103a {n} \u1000\u102d\u102f \u1021\u101b\u1004\u103a \u1015\u103c\u102e\u1038\u1021\u1031\u102c\u1004\u103a \u101c\u102f\u1015\u103a\u1015\u102b",
        tier_free_label: "Free",
        toast_recap_over_hint_free: "FREE_LIMIT_HINT",
        toast_recap_over_limit: "OVER_LIMIT|{tier}|{m}|{hint}",
        clone_check_ok: "OK {kb} KB",
    },
};
function t(k) { return (I18N_MAP[LANG] && I18N_MAP[LANG][k]) || k; }
function tf(k, p = {}) {
    let s = t(k);
    for (const [key, val] of Object.entries(p)) s = s.split("{" + key + "}").join(String(val));
    return s;
}
const TOASTS = [];
function showStudioToast(msg, type, dur) { TOASTS.push({ msg, type, dur }); }
let HAS_SOURCE = false;
let GATE_STATE = "clear";
const state = {
    recapSourceDuration: 0,
    user: { is_vip: false, is_admin: false },
    recapStepsSourceFlowDone: false,
    recapStepsInitialised: false,
    selectedReferenceFile: null,
    customCloneStep: 1,
};
function recapHasSource() { return HAS_SOURCE; }
function recapNormalMaxSeconds() { return 120; }
function recapDurationGateState() { return GATE_STATE; }
function refreshStickyCtas() {}
function refreshRecapStep4Choices() {}
function refreshRecapStep4Output() {}
function refreshRecapStep4Eta() {}
function updateRecapSplitCostHint() {}
function refreshRecapStep2VideoFit() {}
function syncTelegramBackButton() {}
"""


def test_i18n_recap_step_locked_hint_parity_and_copy():
    assert 'recap_step_locked_hint: "အဆင့် {n} ကို အရင် ပြီးအောင် လုပ်ပါ",' in I18N_JS
    assert 'recap_step_locked_hint: "Finish step {n} first",' in I18N_JS


def test_recap_locked_css_states_and_zero_backdrop_filter():
    assert '#tab-recap .recap-steps-nav-item[aria-disabled="true"]' in STYLE_CSS
    assert 'html[data-theme="dark"] #tab-recap .recap-steps-nav-item[aria-disabled="true"]' in STYLE_CSS
    rule = re.search(r'#tab-recap \.recap-steps-nav-item\[aria-disabled="true"\]\{([^}]+)\}', STYLE_CSS)
    assert rule
    body = rule.group(1)
    assert "opacity:" in body or "opacity: " in body
    assert "not-allowed" in body
    assert "dashed" in body
    p3_start = STYLE_CSS.index("VCP UI REBUILD V2 — PHASE 3")
    p3_end = STYLE_CSS.index("VCP UI REBUILD V2 — PHASE 4")
    assert "backdrop-filter" not in STYLE_CSS[p3_start:p3_end]


def test_recap_gating_behavior_node(tmp_path):
    script = _NODE_DOM_HARNESS + _extract_wizard_slice() + r"""
initRecapSteps();
const snapInit = {
    step: recapCurrentStep,
    locked: navItems.map(b => b.hasAttribute("aria-disabled")),
    clsLocked: navItems.map(b => b.classList.contains("is-locked")),
};

// 1. Tapping locked Step 2 or Step 4 before Step 1 completes must NOT navigate.
navItems[1].click();
const afterLocked2En = { step: recapCurrentStep, toast: TOASTS.at(-1)?.msg };
navItems[3].click();
const afterLocked4En = { step: recapCurrentStep, toast: TOASTS.at(-1)?.msg };

// Burmese locale hint check + data-step-next="3" without source + programmatic call
LANG = "my";
nextBtns[0].click();
const afterNext3My = { step: recapCurrentStep, toast: TOASTS.at(-1)?.msg };
showRecapStep(4);
const afterProg4My = { step: recapCurrentStep, toast: TOASTS.at(-1)?.msg };

// 2. Completing Step 1 unlocks Step 2 and advances cleanly; Steps 3-4 stay locked.
LANG = "en";
HAS_SOURCE = true;
syncRecapStepsWithSource(true);
const snapStep2 = {
    step: recapCurrentStep,
    done1: RECAP_STEP_DONE[1],
    locked: navItems.map(b => b.hasAttribute("aria-disabled")),
    cardsHidden: [1, 2, 3, 4].map(n => stepCards[n].classList.contains("hidden")),
};

// 3. Tapping Step 4 while Step 2 incomplete names Step 2 (first incomplete step).
navItems[3].click();
const afterLocked4From2 = { step: recapCurrentStep, toast: TOASTS.at(-1)?.msg };

// 4. Continue on Step 2 with source earns Step 2 and reaches Step 3; Step 1 & 2 revisitable.
nextBtns[0].click();
const snapStep3 = {
    step: recapCurrentStep,
    done2: RECAP_STEP_DONE[2],
    locked: navItems.map(b => b.hasAttribute("aria-disabled")),
};
navItems[0].click(); // revisit completed Step 1
const afterRevisit1 = { step: recapCurrentStep };
navItems[2].click(); // return to current first-incomplete Step 3
const afterReturn3 = { step: recapCurrentStep };

// 5. Continue on Step 3 completes Step 3 and opens Step 4; Step 4 never gets RECAP_STEP_DONE[4]=true.
nextBtns[1].click();
const snapStep4 = {
    step: recapCurrentStep,
    done3: RECAP_STEP_DONE[3],
    done4: RECAP_STEP_DONE[4],
    locked: navItems.map(b => b.hasAttribute("aria-disabled")),
};

// 6. Batch CI free-blocked on Step 1 still runs before step-order gating.
showRecapStep(1);
GATE_STATE = "free-blocked";
navItems[1].click();
const afterCiBlock = { step: recapCurrentStep, toast: TOASTS.at(-1) };

console.log(JSON.stringify({
    snapInit,
    afterLocked2En,
    afterLocked4En,
    afterNext3My,
    afterProg4My,
    snapStep2,
    afterLocked4From2,
    snapStep3,
    afterRevisit1,
    afterReturn3,
    snapStep4,
    afterCiBlock,
}));
"""
    out = _run_node(script, tmp_path)

    assert out["snapInit"]["step"] == 1
    assert out["snapInit"]["locked"] == [False, True, True, True]
    assert out["snapInit"]["clsLocked"] == [False, True, True, True]

    assert out["afterLocked2En"] == {"step": 1, "toast": "Finish step 1 first"}
    assert out["afterLocked4En"] == {"step": 1, "toast": "Finish step 1 first"}
    assert out["afterNext3My"] == {"step": 1, "toast": "အဆင့် 1 ကို အရင် ပြီးအောင် လုပ်ပါ"}
    assert out["afterProg4My"] == {"step": 1, "toast": "အဆင့် 1 ကို အရင် ပြီးအောင် လုပ်ပါ"}

    assert out["snapStep2"]["step"] == 2
    assert out["snapStep2"]["done1"] is True
    assert out["snapStep2"]["locked"] == [False, False, True, True]
    assert out["snapStep2"]["cardsHidden"] == [True, False, True, True]

    assert out["afterLocked4From2"] == {"step": 2, "toast": "Finish step 2 first"}

    assert out["snapStep3"]["step"] == 3
    assert out["snapStep3"]["done2"] is True
    assert out["snapStep3"]["locked"] == [False, False, False, True]
    assert out["afterRevisit1"] == {"step": 1}
    assert out["afterReturn3"] == {"step": 3}

    assert out["snapStep4"]["step"] == 4
    assert out["snapStep4"]["done3"] is True
    assert out["snapStep4"]["done4"] is False
    assert out["snapStep4"]["locked"] == [False, False, False, False]

    assert out["afterCiBlock"]["step"] == 1
    assert out["afterCiBlock"]["toast"]["msg"].startswith("OVER_LIMIT|Free|2|")
    assert out["afterCiBlock"]["toast"]["dur"] == 6000


def test_voice_clone_stepper_retired_in_round13():
    # Round 13 retired #custom-clone-stepper while keeping Phase 4 zero-backdrop CSS budget intact.
    assert 'id="custom-clone-stepper"' not in INDEX_HTML
    p4_start = STYLE_CSS.index("VCP UI REBUILD V2 — PHASE 4")
    p4_end = STYLE_CSS.index("VCP UI REBUILD V2 — PHASE 5")
    p4 = STYLE_CSS[p4_start:p4_end]
    assert "backdrop-filter" not in p4

