"""Batch CI — recap duration cap: warn + gate at the earliest step,
persistent explanation (2026-11-07).

Diagnosed 2026-11-07 from a real FREE user with a 2:01 source: the
FREE 120 s / VIP 600 s SOURCE cap was enforced only at final submit
(api.py `_recap_submit_duration_decision` → HTTP 400), where the
reason reached the user as a seconds-long toast. The wizard never
warned in Steps 1–3 and Step 4 rendered the over-limit duration
with a normal band-1 quote, so the user invested minutes, pressed
1-Click, and could not tell why nothing happened (no credit is
charged — the rejection precedes deduction — but nothing on screen
said so).

CI is client-only (the server checks stay as the backstop):

* A pure decision helper (`recapDurationGateState`) + painter
  (`updateRecapDurationGate`) evaluate the cap the moment the
  client learns the duration — both learn points (upload
  completion, link extract) and the info-card clear paint through
  `updateRecapSplitCostHint`, which now calls the painter; the
  history-prefill path and the extract-failure branch call it
  directly.
* FREE over-limit: persistent notice on Step 1 (shared
  over-limit facts + VIP upsell), advancing past Step 1 refused
  in `showRecapStep`, Step-4 1-Click disabled (the sticky CTA
  mirrors the disabled state) with the reason under the button.
* VIP over-limit: persistent Step-1 notice with split guidance,
  never a block. Admins exempt.
* A server duration rejection the client could not pre-empt
  (unknown-duration link source) is ALSO rendered persistently
  under the Step-4 CTA, identified by the same marker
  mapErrorToToast's recap pass-through family uses.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")

DURATION_MARKER = "ကန့်သတ်ချက် (အများဆုံး"


def _slice(start_marker: str, end_marker: str) -> str:
    start = APP_JS.index(start_marker)
    end = APP_JS.index(end_marker, start)
    return APP_JS[start:end]


def _gate_slice() -> str:
    """The CI block: pure helper + duration text + notice text +
    painter, plus the pre-existing recapNormalMaxSeconds it reads."""
    head = _slice("function recapNormalMaxSeconds()", "function enableRecapSplitForLongVideo")
    body = _slice(
        "function recapDurationGateState(",
        "window.updateRecapDurationGate = updateRecapDurationGate;",
    )
    return head + body


# ── Markup: the two persistent notice elements ────────────────────

def test_step1_notice_element_inside_step1_card():
    i_step1 = INDEX_HTML.index('id="recap-step-1"')
    i_note = INDEX_HTML.index('id="recap-overlimit-notice"')
    i_step2 = INDEX_HTML.index('id="recap-step-2"')
    assert i_step1 < i_note < i_step2
    tag = INDEX_HTML[i_note - 30:i_note + 130]
    assert "recap-overlimit-notice" in tag and 'role="alert"' in tag


def test_step4_notice_element_under_the_cta():
    i_btn = INDEX_HTML.index('id="btn-start-recap"')
    i_note = INDEX_HTML.index('id="recap-overlimit-notice-step4"')
    i_unverified = INDEX_HTML.index('id="recap-unverified-warning"')
    assert i_btn < i_note < i_unverified
    tag = INDEX_HTML[i_note - 30:i_note + 140]
    assert "recap-overlimit-notice" in tag and 'role="alert"' in tag


def test_notice_css_is_the_alert_family():
    m = re.search(r"\.recap-overlimit-notice\s*\{([^}]+)\}", STYLE_CSS)
    assert m, ".recap-overlimit-notice rule missing"
    rule = m.group(1)
    assert "rgba(239, 68, 68, 0.08)" in rule  # upload-error-box tint
    assert "rgba(239, 68, 68, 0.35)" in rule  # upload-error-box border
    assert "var(--s2)" in rule and "var(--s3)" in rule  # spacing rhythm


# ── i18n: the notice key exists in both languages, fully templated ─

def test_notice_key_both_languages_with_all_placeholders():
    lines = [ln for ln in I18N_JS.splitlines() if "recap_over_limit_notice:" in ln]
    assert len(lines) == 2, lines
    for ln in lines:
        for ph in ("{duration}", "{tier}", "{max}", "{hint}"):
            assert ph in ln, (ph, ln)


# ── Wiring pins ───────────────────────────────────────────────────

def test_gate_helpers_and_painter_exist():
    assert "function recapDurationGateState(durationSeconds, maxSeconds, isVip, isAdmin)" in APP_JS
    assert "function updateRecapDurationGate()" in APP_JS
    assert "window.updateRecapDurationGate = updateRecapDurationGate;" in APP_JS
    # Caps come from the /user/me payload via the existing helper —
    # the gate never hardcodes 120/600 itself.
    gate = _slice("function updateRecapDurationGate()", "window.updateRecapDurationGate")
    assert "recapNormalMaxSeconds()" in gate
    assert "120" not in gate and "600" not in gate


def test_cost_hint_hub_repaints_the_gate():
    body = _slice("function updateRecapSplitCostHint()", "window.repaintRecapCtaPrice")
    assert "updateRecapDurationGate();" in body


def test_show_recap_step_refuses_free_blocked_advance():
    body = _slice("function showRecapStep(n)", "window.showRecapStep")
    guard = body.index('=== "free-blocked"')
    assign = body.index("recapCurrentStep = n;")
    assert guard < assign, "the refusal must precede the step switch"
    assert "showStudioToast" in body[guard:assign]


def test_submit_catch_persists_duration_rejection_by_mapper_marker():
    body = _slice("const mapped = mapErrorToToast", "function renderRecapFormProgress")
    assert f"rawErr.includes('{DURATION_MARKER}')" in APP_JS
    assert "recapSubmitRejectText = rawErr.includes" in APP_JS
    # The painter is re-asserted after the catch re-enables the button.
    assert body.index("updateRecapDurationGate();") > body.index("btn.disabled = false")


def test_submit_success_and_new_source_clear_the_rejection():
    assert APP_JS.count('recapSubmitRejectText = "";') >= 2  # success + clearRecapInfoCard
    clear = _slice("function clearRecapInfoCard()", "window.clearRecapInfoCard")
    assert 'recapSubmitRejectText = "";' in clear


def test_prefill_and_extract_failure_repaint_the_gate():
    prefill = _slice("state.recapSourceDuration = cfg.duration_seconds || null;",
                     "const srcUrl = (cfg.url")
    assert "updateRecapDurationGate();" in prefill
    failure = _slice("state.lastRecapInfo = { metadata_verified: false",
                     "const msg = err.message")
    assert "updateRecapDurationGate();" in failure


def test_language_repaint_renders_the_gate():
    body = _slice("function refreshRecapInfoMetaLanguage()", "window.refreshRecapInfoMetaLanguage")
    assert "updateRecapDurationGate();" in body


# ── Behaviour: the pure decision helper (node) ────────────────────

def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    tmp = ROOT / "tests/unit/_ci_harness_tmp.js"
    try:
        tmp.write_text(script, encoding="utf-8")
        res = subprocess.run([node, str(tmp)], capture_output=True, text=True, timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_gate_state_matrix_node():
    script = (
        _gate_slice()
        + "\nconst cases = {"
        + " free_over: recapDurationGateState(121, 120, false, false),"
        + " free_at: recapDurationGateState(120, 120, false, false),"
        + " free_under: recapDurationGateState(119.5, 120, false, false),"
        + " vip_over: recapDurationGateState(601, 600, true, false),"
        + " vip_at: recapDurationGateState(600, 600, true, false),"
        + " admin_over: recapDurationGateState(9999, 120, false, true),"
        + " unknown_zero: recapDurationGateState(0, 120, false, false),"
        + " unknown_null: recapDurationGateState(null, 120, false, false),"
        + " unknown_nan: recapDurationGateState(NaN, 120, false, false),"
        + " unknown_inf: recapDurationGateState(Infinity, 120, false, false),"
        + " dur_text: recapGateDurationText(121),"
        + " dur_text_pad: recapGateDurationText(65),"
        + " };\nconsole.log(JSON.stringify(cases));\n"
    )
    out = _run_node(script)
    assert out["free_over"] == "free-blocked"
    assert out["free_at"] == "clear"
    assert out["free_under"] == "clear"
    assert out["vip_over"] == "vip-notice"
    assert out["vip_at"] == "clear"
    assert out["admin_over"] == "clear"
    assert out["unknown_zero"] == "clear"
    assert out["unknown_null"] == "clear"
    assert out["unknown_nan"] == "clear"
    assert out["unknown_inf"] == "clear"
    assert out["dur_text"] == "2:01"
    assert out["dur_text_pad"] == "1:05"


# ── Behaviour: the painter against a stub DOM (node) ─────────────

_DOM_STUB = r"""
function makeEl() {
    const el = {
        innerText: "", disabled: false, style: {}, _attrs: {}, _classes: new Set(["hidden"]),
    };
    el.classList = {
        add: (c) => el._classes.add(c),
        remove: (c) => el._classes.delete(c),
        toggle: (c, force) => { if (force) el._classes.add(c); else el._classes.delete(c); },
        contains: (c) => el._classes.has(c),
    };
    el.setAttribute = (k, v) => { el._attrs[k] = v; };
    el.getAttribute = (k) => (k in el._attrs ? el._attrs[k] : null);
    el.removeAttribute = (k) => { delete el._attrs[k]; };
    return el;
}
const ELS = {
    "recap-overlimit-notice": makeEl(),
    "recap-overlimit-notice-step4": makeEl(),
    "btn-start-recap": makeEl(),
};
let JOB_ACTIVE = false;
let STICKY_CALLS = 0;
let state = { user: null, recapSourceDuration: 0 };
const document = { getElementById: (id) => ELS[id] || null };
function t(k) { return { tier_free_label: "FREE_LABEL",
    toast_recap_over_hint_free: "HINT_FREE",
    toast_recap_over_hint_vip: "HINT_VIP" }[k] || k; }
function tf(k, p) { return k + "|" + JSON.stringify(p); }
function recapJobIsActive() { return JOB_ACTIVE; }
function refreshStickyCtas() { STICKY_CALLS++; }
"""


def _paint(script_tail: str) -> dict:
    return _run_node(_DOM_STUB + _gate_slice() + script_tail)


def test_painter_free_blocked_then_cleared_node():
    out = _paint(r"""
state.user = { is_vip: false, is_admin: false };
state.recapSourceDuration = 121;
const g1 = updateRecapDurationGate();
const snap1 = {
    gate: g1,
    s1_hidden: ELS["recap-overlimit-notice"].classList.contains("hidden"),
    s1_text: ELS["recap-overlimit-notice"].innerText,
    s4_hidden: ELS["recap-overlimit-notice-step4"].classList.contains("hidden"),
    btn_disabled: ELS["btn-start-recap"].disabled,
    btn_flag: ELS["btn-start-recap"].getAttribute("data-ci-gate-disabled"),
    sticky: STICKY_CALLS,
};
state.recapSourceDuration = 100;
const g2 = updateRecapDurationGate();
const snap2 = {
    gate: g2,
    s1_hidden: ELS["recap-overlimit-notice"].classList.contains("hidden"),
    s4_hidden: ELS["recap-overlimit-notice-step4"].classList.contains("hidden"),
    btn_disabled: ELS["btn-start-recap"].disabled,
    btn_flag: ELS["btn-start-recap"].getAttribute("data-ci-gate-disabled"),
};
console.log(JSON.stringify({ snap1, snap2 }));
""")
    s1, s2 = out["snap1"], out["snap2"]
    assert s1["gate"] == "free-blocked"
    assert s1["s1_hidden"] is False and s1["s4_hidden"] is False
    assert '"duration":"2:01"' in s1["s1_text"] and '"max":2' in s1["s1_text"]
    assert "HINT_FREE" in s1["s1_text"]
    assert s1["btn_disabled"] is True and s1["btn_flag"] == "1"
    assert s1["sticky"] >= 1  # sticky CTA mirror refreshed
    assert s2["gate"] == "clear"
    assert s2["s1_hidden"] is True and s2["s4_hidden"] is True
    assert s2["btn_disabled"] is False and s2["btn_flag"] is None


def test_painter_vip_notice_only_and_admin_exempt_node():
    out = _paint(r"""
state.user = { is_vip: true, is_admin: false };
state.recapSourceDuration = 601;
const gVip = updateRecapDurationGate();
const vip = {
    gate: gVip,
    s1_hidden: ELS["recap-overlimit-notice"].classList.contains("hidden"),
    s1_text: ELS["recap-overlimit-notice"].innerText,
    s4_hidden: ELS["recap-overlimit-notice-step4"].classList.contains("hidden"),
    btn_disabled: ELS["btn-start-recap"].disabled,
};
state.user = { is_vip: false, is_admin: true };
state.recapSourceDuration = 9999;
const gAdm = updateRecapDurationGate();
const adm = {
    gate: gAdm,
    s1_hidden: ELS["recap-overlimit-notice"].classList.contains("hidden"),
    btn_disabled: ELS["btn-start-recap"].disabled,
};
console.log(JSON.stringify({ vip, adm }));
""")
    vip, adm = out["vip"], out["adm"]
    assert vip["gate"] == "vip-notice"
    assert vip["s1_hidden"] is False and "HINT_VIP" in vip["s1_text"]
    assert vip["s4_hidden"] is True and vip["btn_disabled"] is False
    assert adm["gate"] == "clear"
    assert adm["s1_hidden"] is True and adm["btn_disabled"] is False


def test_painter_submit_rejection_persists_until_cleared_node():
    out = _paint(r"""
state.user = { is_vip: false, is_admin: false };
state.recapSourceDuration = 0;  // duration never learned (link source)
recapSubmitRejectText = "SERVER OVER-LIMIT MESSAGE";
updateRecapDurationGate();
const shown = {
    s4_hidden: ELS["recap-overlimit-notice-step4"].classList.contains("hidden"),
    s4_text: ELS["recap-overlimit-notice-step4"].innerText,
    btn_disabled: ELS["btn-start-recap"].disabled,
};
recapSubmitRejectText = "";
updateRecapDurationGate();
const cleared = {
    s4_hidden: ELS["recap-overlimit-notice-step4"].classList.contains("hidden"),
};
console.log(JSON.stringify({ shown, cleared }));
""")
    assert out["shown"]["s4_hidden"] is False
    assert out["shown"]["s4_text"] == "SERVER OVER-LIMIT MESSAGE"
    assert out["shown"]["btn_disabled"] is False  # rejection note never blocks by itself
    assert out["cleared"]["s4_hidden"] is True


def test_painter_never_undoes_another_owners_disable_node():
    out = _paint(r"""
state.user = { is_vip: false, is_admin: false };
ELS["btn-start-recap"].disabled = true;  // another owner (e.g. upload error)
state.recapSourceDuration = 121;
updateRecapDurationGate();               // gate blocks, but did not disable it itself
const during = { flag: ELS["btn-start-recap"].getAttribute("data-ci-gate-disabled") };
state.recapSourceDuration = 60;
updateRecapDurationGate();               // gate clears — must NOT re-enable
console.log(JSON.stringify({ during,
    btn_disabled: ELS["btn-start-recap"].disabled }));
""")
    assert out["during"]["flag"] is None
    assert out["btn_disabled"] is True


# ── Cache token ───────────────────────────────────────────────────

def test_cache_version_r106():
    assert INDEX_HTML.count("?v=20261104_r140") == 4
