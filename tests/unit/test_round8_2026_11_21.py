"""Round 8 tests (2026-11-21): Voice sample card + TTS tab cleanup + Saved voice for everyone."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.config import settings
from app.models.schema import Job
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
APP_JS = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")


def _extract_fn(src: str, name: str) -> str:
    m = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", src)
    assert m, f"Missing function {name}"
    start = m.start()
    brace = src.index("{", m.end())
    depth = 0
    in_sq = in_dq = in_bt = False
    esc = False
    i = brace
    while i < len(src):
        ch = src[i]
        if esc:
            esc = False
        elif in_sq:
            if ch == "\\":
                esc = True
            elif ch == "'":
                in_sq = False
        elif in_dq:
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_dq = False
        elif in_bt:
            if ch == "\\":
                esc = True
            elif ch == "`":
                in_bt = False
        else:
            if ch == "'":
                in_sq = True
            elif ch == '"':
                in_dq = True
            elif ch == "`":
                in_bt = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return src[start : i + 1]
        i += 1
    raise AssertionError(f"Unclosed function {name}")


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


async def _make_user(db_session, telegram_id: int, **fields):
    user, _ = await get_or_create_user(db_session, telegram_id=telegram_id)
    for k, v in fields.items():
        setattr(user, k, v)
    await db_session.commit()
    await db_session.refresh(user)
    return user


# ── Part A: Voice tab ref-audio card & title cleanup ──────────────────────────

def test_round8_part_a_voice_sample_card_and_no_custom_ref_title():
    assert 'data-i18n="custom_ref_title"' not in INDEX_HTML
    assert "custom_ref_title" not in I18N_JS

    ref_sec = INDEX_HTML.split('id="ref-audio-section"', 1)[1].split("<!-- Step 2:", 1)[0]
    assert 'id="ref-sample-card"' in ref_sec
    assert 'class="ref-sample-card"' in ref_sec
    for child_id in ("saved-custom-voice-bar", "ref-upload-row", "ref-notes-details", "custom-clone-check-status"):
        assert child_id in ref_sec
        assert ref_sec.index('id="ref-sample-card"') < ref_sec.index(child_id)

    for marker in (
        "#tab-voice #ref-audio-section",
        "#tab-voice .ref-sample-card",
        'html[data-theme="dark"] #tab-voice .ref-sample-card',
        "html.lite #tab-voice .ref-sample-card",
        'html[data-theme="dark"].lite #tab-voice .ref-sample-card',
    ):
        assert marker in STYLE_CSS, f"Missing CSS selector: {marker}"


# ── Part B: TTS tab cleanup, empty box, single-row chips, Profile BYOK ────────

def test_round8_part_b_retired_markers_and_keys_removed():
    tr = _translations()
    removed_keys = (
        "custom_ref_title",
        "tts_title",
        "tts_subtitle",
        "tts_sample_text",
        "tts_balance_loading",
        "tts_balance_error",
        "tts_balance_line",
        "tts_balance_line_unlimited",
        "tts_balance_line_admin",
        "tts_balance_line_vip",
        "tts_balance_line_vip_ref",
        "tts_balance_line_paid",
        "tts_balance_line_free_daily",
        "voice_tts_balance_line",
    )
    for key in removed_keys:
        assert key not in tr["my"], f"Dead i18n key in my: {key}"
        assert key not in tr["en"], f"Dead i18n key in en: {key}"
        assert not re.search(rf"(?<![A-Za-z0-9_]){re.escape(key)}\s*:", I18N_JS), f"Dead i18n key in source: {key}"

    for dead_html_id in (
        'id="tts-balance-line"',
        'id="tts-gemini-key-card"',
        'id="tts-byok-advanced-toggle"',
        'id="tts-byok-advanced-body"',
        'id="btn-tts-key-save"',
        'id="btn-tts-key-delete"',
    ):
        assert dead_html_id not in INDEX_HTML

    tts_pane = INDEX_HTML.split('<section id="tab-tts"', 1)[1].split("</section>", 1)[0]
    assert 'class="pane-header"' not in tts_pane
    cta_block = tts_pane.split('id="tts-cta-block"', 1)[1].split('<div class="card glass output-card', 1)[0]
    assert "step-badge-title" not in cta_block
    for keep_marker in ('id="tts-preflight"', 'id="btn-generate-tts"', "queue-notice-banner"):
        assert keep_marker in cta_block
    assert 'id="tts-text-input"' in tts_pane
    assert re.search(r'<textarea[^>]*id="tts-text-input"[^>]*></textarea>', tts_pane)

    for dead_js in (
        "maybePrefillTtsSample",
        "ttsSampleSettled",
        "vcp_tts_draft",
        "formatTtsTabBalanceLine",
        "paintTtsBalanceLine",
        "paintTtsQuotaError",
        "paintTtsQuotaInstant",
        "paintVoiceTtsBalanceLine",
        "voice-tts-balance-line",
        "tts-balance-line",
        "tts-gemini-key-card",
        "saveTtsGeminiKey",
        "deleteTtsGeminiKey",
        "toggleTtsByokAdvanced",
    ):
        assert dead_js not in APP_JS, f"Dead JS symbol still present: {dead_js}"


def test_round8_part_b_sample_chips_bottom_spacing_hints_and_edge_labels():
    tr = _translations()

    # B3: single-row TTS sample chips CSS scoped to #tab-tts
    chips_block = STYLE_CSS.split("#tab-tts .sample-chips {", 1)[1].split("}", 1)[0]
    assert "flex-wrap: nowrap;" in chips_block
    assert "overflow-x: auto;" in chips_block

    # B8: compact bottom spacing
    assert ".content-viewport:has(#tab-tts.active)" in STYLE_CSS
    assert re.search(r"#tab-tts\s*>\s*\.hub-link\s*\{\s*margin-top:\s*0;", STYLE_CSS)

    # B6: exact trimmed tts_cost_hint_more strings in MY and EN
    expected_my_more = (
        "တစ်နေ့ အခမဲ့ 15 မိနစ် (VIP 60 မိနစ်) · တစ်ကြိမ် စာလုံး 1,000 (VIP 5,000) အထိ ရပါတယ်။ "
        "Gemini အသံ အခမဲ့ မြည်းစမ်းခွင့် တစ်နေ့ 2 မိနစ် (ကိုယ်ပိုင် Key ထည့်ထားရင် အကန့်အသတ်မရှိ)။"
    )
    expected_en_more = (
        "Daily free TTS: 15 min (VIP 60 min) · Up to 1,000 characters per generation (VIP 5,000). "
        "Gemini free taste: 2 min/day (unlimited with your own key)."
    )
    assert tr["my"]["tts_cost_hint_more"] == expected_my_more
    assert tr["en"]["tts_cost_hint_more"] == expected_en_more
    assert "Admin" in tr["my"]["tts_cost_hint_more_admin"] and "{max_chars}" in tr["my"]["tts_cost_hint_more_admin"]
    assert "Admin" in tr["en"]["tts_cost_hint_more_admin"] and "{max_chars}" in tr["en"]["tts_cost_hint_more_admin"]
    assert tr["my"]["tts_cost_hint_more_vip"] == tr["my"]["tts_cost_hint_more_vip_ref"]
    assert tr["en"]["tts_cost_hint_more_vip"] == tr["en"]["tts_cost_hint_more_vip_ref"]
    assert f'data-i18n="tts_cost_hint_more">{expected_my_more}</p>' in INDEX_HTML

    # B7: Profile BYOK strings & kept tts_key_* keys
    assert "Profile" in tr["my"]["tts_taste_spent"]
    assert "Profile" in tr["en"]["tts_taste_spent"]
    assert "Profile" in tr["my"]["tts_key_prompt_body"]
    assert "Profile" in tr["en"]["tts_key_prompt_body"]
    assert 'data-i18n="tts_key_prompt_body"' in INDEX_HTML
    assert "Profile ထဲမှာ ကိုယ်ပိုင် Gemini Key" in INDEX_HTML

    for kept_key in (
        "tts_key_card_title",
        "tts_key_state_none",
        "tts_key_state_ok",
        "tts_key_state_invalid",
        "tts_key_input_placeholder",
        "tts_key_save_btn",
        "tts_key_delete_btn",
        "tts_key_saving",
        "tts_key_saved",
        "tts_key_deleted",
        "tts_key_save_failed",
        "tts_key_guide_title",
        "tts_key_guide_1",
        "tts_key_guide_2",
        "tts_key_guide_3",
        "tts_key_honest_1",
        "tts_key_honest_2",
        "tts_key_active_badge",
        "tts_key_prompt_title",
        "tts_key_prompt_body",
        "tts_key_prompt_add",
        "tts_key_prompt_edge",
    ):
        assert kept_key in tr["my"] and kept_key in tr["en"], kept_key

    # B9: 8 simplified Edge TTS voice labels
    expected_my_edge = {
        "tts_edge_thiha": "ကိုသီဟ (ကျား)",
        "tts_edge_nilar": "မနီလာ (မ)",
        "tts_edge_andrew": "ကိုအန်ဒရူး (ကျား)",
        "tts_edge_brian": "ကိုဘရိုင်ယန် (ကျား)",
        "tts_edge_remy": "ကိုရီမီ (ကျား)",
        "tts_edge_ava": "မအေဗာ (မ)",
        "tts_edge_emma": "မအမ်မာ (မ)",
        "tts_edge_vivienne": "မဗီဗီယန် (မ)",
    }
    expected_en_edge = {
        "tts_edge_thiha": "Thiha (M)",
        "tts_edge_nilar": "Nilar (F)",
        "tts_edge_andrew": "Andrew (M)",
        "tts_edge_brian": "Brian (M)",
        "tts_edge_remy": "Remy (M)",
        "tts_edge_ava": "Ava (F)",
        "tts_edge_emma": "Emma (F)",
        "tts_edge_vivienne": "Vivienne (F)",
    }
    for k, v in expected_my_edge.items():
        assert tr["my"][k] == v
        assert f'data-i18n="{k}">{v}<' in INDEX_HTML
    for k, v in expected_en_edge.items():
        assert tr["en"][k] == v


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_round8_js_behaviour_under_node():
    fn_switch = _extract_fn(APP_JS, "switchTab")
    fn_key_state = _extract_fn(APP_JS, "paintTtsGeminiKeyState")
    fn_apply_mut = _extract_fn(APP_JS, "_applyGeminiKeyMutationView")
    fn_save_key = _extract_fn(APP_JS, "saveGeminiKeyFrom")
    fn_del_key = _extract_fn(APP_JS, "deleteGeminiKeyFrom")
    fn_prompt_add = _extract_fn(APP_JS, "_cgPromptAddKey")

    combined = "\n".join([fn_switch, fn_key_state, fn_apply_mut, fn_save_key, fn_del_key, fn_prompt_add])
    script = f"""
const vm = require('vm');
function mkEl(id) {{
    const s = new Set(['hidden']);
    const el = {{
        id,
        value: '',
        textContent: '',
        disabled: false,
        focused: false,
        scrolled: false,
        dataset: {{}},
        attrs: {{}},
        style: {{}},
        classList: {{
            add: (...c) => c.forEach(x => s.add(x)),
            remove: (...c) => c.forEach(x => s.delete(x)),
            toggle: (c, f) => {{ const w = f === undefined ? !s.has(c) : !!f; if (w) s.add(c); else s.delete(c); return w; }},
            contains: c => s.has(c),
        }},
        setAttribute: (k, v) => {{ el.attrs[k] = String(v); }},
        removeAttribute: (k) => {{ delete el.attrs[k]; }},
        scrollIntoView: () => {{ el.scrolled = true; }},
        focus: () => {{ el.focused = true; }},
    }};
    return el;
}}

const els = {{
    'tab-voice': mkEl('tab-voice'),
    'tab-tts': mkEl('tab-tts'),
    'tts-text-input': mkEl('tts-text-input'),
    'profile-gemini-key-card': mkEl('profile-gemini-key-card'),
    'profile-key-input': mkEl('profile-key-input'),
    'profile-key-state': mkEl('profile-key-state'),
    'profile-key-active-badge': mkEl('profile-key-active-badge'),
    'btn-profile-key-save': mkEl('btn-profile-key-save'),
    'btn-profile-key-delete': mkEl('btn-profile-key-delete'),
    'tts-key-prompt-modal': mkEl('tts-key-prompt-modal'),
}};
// Only #tab-tts & Profile controls exist; NO #tts-gemini-key-card / #tts-balance-line!
let profileOpened = 0;
let toasts = [];
let fetchCalls = [];
const windowObj = {{
    refreshTtsQuota: () => {{}},
    syncAllStickyCtas: () => {{}},
    scrollTo: () => {{}},
}};
const ctx = {{
    window: windowObj,
    localStorage: {{ getItem: () => 'my', setItem: () => {{}} }},
    state: {{ currentTab: 'voice', user: {{ is_admin: false }} }},
    _cgQuotaCache: null,
    _cgTasteState: null,
    _lastTtsQuota: null,
    pauseInactiveTabMedia: () => {{}},
    markPaneEnter: () => {{}},
    refreshStickyCtas: () => {{}},
    moveNavPill: () => {{}},
    moveSpeedPills: () => {{}},
    moveSegPills: () => {{}},
    paintProfileView: () => {{}},
    syncTabAdminLifecycle: () => {{}},
    syncTelegramBackButton: () => {{}},
    openProfileView: () => {{ profileOpened++; }},
    closeProfileView: () => {{}},
    closeMoreMenu: () => {{}},
    refreshEdgeQuota: () => {{}},
    loadUserProfile: () => Promise.resolve(),
    loadRecapMeta: () => {{}},
    initHubTab: () => {{}},
    loadAdminData: () => {{}},
    paintTtsTasteNote: () => {{}},
    paintTtsCostHintTiered: () => {{}},
    refreshPlanSurfaceFromProfile: () => {{}},
    setTimeout: (fn) => {{ fn(); return 1; }},
    showToast: (msg, type) => toasts.push({{ msg, type }}),
    showStudioToast: (msg, type) => toasts.push({{ msg, type }}),
    showStudioConfirm: (msg, ok) => {{ if (typeof ok === 'function') ok(); }},
    getAuthHeaders: () => ({{}}),
    fetchWithTimeout: async (url, opts) => {{
        fetchCalls.push({{ url, method: opts && opts.method }});
        if (url === '/api/tts/gemini-key' && opts.method === 'POST') {{
            return {{ ok: true, status: 200, json: async () => ({{ has_key: true, state: 'ok', masked: '...1234' }}) }};
        }}
        return {{ ok: true, status: 200, json: async () => ({{ has_key: false, state: 'none', masked: null }}) }};
    }},
    t: (k) => k,
    tf: (k, a) => k + ':' + JSON.stringify(a || {{}}),
    document: {{
        getElementById: (id) => els[id] || null,
        querySelectorAll: () => [],
        querySelector: () => null,
        documentElement: {{ setAttribute: () => {{}} }},
        body: {{ classList: {{ toggle: () => {{}} }} }},
    }},
}};
vm.runInNewContext({json.dumps(combined)}, ctx);

(async () => {{
    // 1. switchTab to tab-tts keeps tts-text-input completely empty
    ctx.switchTab('tab-tts');
    const ttsInputAfterSwitch = els['tts-text-input'].value;

    // 2. _cgPromptAddKey opens Profile, scrolls to profile-gemini-key-card, focuses profile-key-input
    els['tts-key-prompt-modal'].classList.remove('hidden');
    ctx._cgPromptAddKey();
    const promptResult = {{
        modalHidden: els['tts-key-prompt-modal'].classList.contains('hidden'),
        profileOpened,
        scrolled: els['profile-gemini-key-card'].scrolled,
        focused: els['profile-key-input'].focused,
    }};

    // 3. Profile key save & delete work cleanly without any TTS card in DOM
    els['profile-key-input'].value = 'AIzaSyFakeTestKey12345';
    await ctx.saveGeminiKeyFrom('profile-key-input', 'btn-profile-key-save');
    const afterSave = {{
        inputCleared: els['profile-key-input'].value === '',
        badgeHidden: els['profile-key-active-badge'].classList.contains('hidden'),
        deleteHidden: els['btn-profile-key-delete'].classList.contains('hidden'),
        stateText: els['profile-key-state'].textContent,
    }};

    await ctx.deleteGeminiKeyFrom('btn-profile-key-delete');
    const afterDelete = {{
        badgeHidden: els['profile-key-active-badge'].classList.contains('hidden'),
        deleteHidden: els['btn-profile-key-delete'].classList.contains('hidden'),
        stateText: els['profile-key-state'].textContent,
        fetchCount: fetchCalls.length,
    }};

    console.log(JSON.stringify({{ ttsInputAfterSwitch, promptResult, afterSave, afterDelete }}));
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["ttsInputAfterSwitch"] == ""
    assert out["promptResult"] == {"modalHidden": True, "profileOpened": 1, "scrolled": True, "focused": True}
    assert out["afterSave"]["inputCleared"] is True
    assert out["afterSave"]["badgeHidden"] is False
    assert out["afterSave"]["deleteHidden"] is False
    assert "tts_key_state_ok" in out["afterSave"]["stateText"]
    assert out["afterDelete"]["badgeHidden"] is True
    assert out["afterDelete"]["deleteHidden"] is True
    assert out["afterDelete"]["stateText"] == "tts_key_state_none"
    assert out["afterDelete"]["fetchCount"] == 2


# ── Part C: Saved voice unlocked for ALL users + distinct 400 errors ──────────

def test_round8_part_c_i18n_and_localize_saved_voice_error():
    tr = _translations()
    assert tr["my"]["toast_saved_voice_not_found"] == "သိမ်းထားတဲ့ အသံကို မတွေ့တော့ပါ — အသံနမူနာ အသစ် ပြန်ရွေးပါ"
    assert tr["en"]["toast_saved_voice_not_found"] == "Your saved voice could not be found — please choose a voice sample again"

    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(STATIC / "js" / "i18n.js"))}, 'utf8');
const ctx = {{
    window: {{}},
    localStorage: {{ getItem: () => 'my', setItem: () => {{}} }},
    document: {{
        querySelectorAll: () => [],
        getElementById: () => null,
        documentElement: {{ setAttribute: () => {{}} }},
        body: {{ classList: {{ toggle: () => {{}} }} }},
    }},
}};
vm.runInNewContext(src, ctx);
ctx.setLanguage('my');
const myMsg = ctx.localizeServerText('သိမ်းထားတဲ့ အသံကို မတွေ့တော့ပါ — အသံနမူနာ အသစ် ပြန်ရွေးပါ');
ctx.setLanguage('en');
const enMsg = ctx.localizeServerText('သိမ်းထားတဲ့ အသံကို မတွေ့တော့ပါ — အသံနမူနာ အသစ် ပြန်ရွေးပါ');
console.log(JSON.stringify({{ myMsg, enMsg }}));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip())
    assert out["myMsg"] == tr["my"]["toast_saved_voice_not_found"]
    assert out["enMsg"] == tr["en"]["toast_saved_voice_not_found"]


@pytest.mark.asyncio
async def test_round8_part_c_free_user_can_use_own_saved_voice_ref(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "admin_ids_raw", "")

    free_user = await _make_user(db_session, 980001, is_admin=False)
    ref_id = f"webapp_ref_u{free_user.id}_aabbccddeeff"
    ref_dir = tmp_path / "temp" / "voxcpm_refs"
    ref_dir.mkdir(parents=True, exist_ok=True)
    sample_file = ref_dir / f"{ref_id}.wav"
    sample_file.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")

    req = api_mod.VoiceGenerateRequest(
        text="မင်္ဂလာပါ သိမ်းထားတဲ့ အသံ စမ်းသပ်မှု",
        engine="voxcpm",
        preset_voice=None,
        voice_ref_id=ref_id,
    )
    resp = await api_mod.create_voice_job(req, user=free_user, db=db_session, _gate=free_user)
    assert resp["success"] is True

    job = await db_session.get(Job, resp["job_id"])
    assert job is not None
    assert job.source_audio_path == str(sample_file)
    assert job.preset_voice is None


@pytest.mark.asyncio
async def test_round8_part_c_cross_user_stale_and_empty_voice_ref_rejected(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)

    owner = await _make_user(db_session, 980002, is_admin=False)
    free_other = await _make_user(db_session, 980003, is_admin=False)
    admin_other = await _make_user(db_session, 980004, is_admin=True)
    monkeypatch.setattr(settings, "admin_ids_raw", "980004")

    owner_ref_id = f"webapp_ref_u{owner.id}_112233445566"
    ref_dir = tmp_path / "temp" / "voxcpm_refs"
    ref_dir.mkdir(parents=True, exist_ok=True)
    (ref_dir / f"{owner_ref_id}.wav").write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")

    expected_bad_ref_msg = "သိမ်းထားတဲ့ အသံကို မတွေ့တော့ပါ — အသံနမူနာ အသစ် ပြန်ရွေးပါ"

    # Cross-user: both FREE user and ADMIN user cannot use owner's voice_ref_id
    for actor in (free_other, admin_other):
        req = api_mod.VoiceGenerateRequest(
            text="မင်္ဂလာပါ",
            engine="voxcpm",
            preset_voice=None,
            voice_ref_id=owner_ref_id,
        )
        with pytest.raises(HTTPException) as exc:
            await api_mod.create_voice_job(req, user=actor, db=db_session, _gate=actor)
        assert exc.value.status_code == 400
        assert exc.value.detail == expected_bad_ref_msg

    # Stale / missing on disk for owner
    stale_req = api_mod.VoiceGenerateRequest(
        text="မင်္ဂလာပါ",
        engine="voxcpm",
        preset_voice=None,
        voice_ref_id=f"webapp_ref_u{owner.id}_000000000000",
    )
    with pytest.raises(HTTPException) as exc_stale:
        await api_mod.create_voice_job(stale_req, user=owner, db=db_session, _gate=owner)
    assert exc_stale.value.status_code == 400
    assert exc_stale.value.detail == expected_bad_ref_msg

    # Malformed ref_id
    bad_shape_req = api_mod.VoiceGenerateRequest(
        text="မင်္ဂလာပါ",
        engine="voxcpm",
        preset_voice=None,
        voice_ref_id="../../etc/passwd",
    )
    with pytest.raises(HTTPException) as exc_bad:
        await api_mod.create_voice_job(bad_shape_req, user=owner, db=db_session, _gate=owner)
    assert exc_bad.value.status_code == 400
    assert exc_bad.value.detail == expected_bad_ref_msg

    # No preset_voice and no voice_ref_id -> still Round 7 no-sample 400
    no_sample_req = api_mod.VoiceGenerateRequest(
        text="မင်္ဂလာပါ",
        engine="voxcpm",
        preset_voice=None,
        voice_ref_id=None,
    )
    with pytest.raises(HTTPException) as exc_empty:
        await api_mod.create_voice_job(no_sample_req, user=owner, db=db_session, _gate=owner)
    assert exc_empty.value.status_code == 400
    assert exc_empty.value.detail == "အသံနမူနာ အရင် ရွေးချယ်ပါ — သို့မဟုတ် သိမ်းထားတဲ့ အသံကို ရွေးပါ"

    count = (await db_session.execute(select(func.count()).select_from(Job))).scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_round8_part_c_curated_preset_and_upload_flow_unchanged(db_session, tmp_path, monkeypatch):
    import io
    import wave
    from fastapi import UploadFile

    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "admin_ids_raw", "")

    free_user_preset = await _make_user(db_session, 980005, is_admin=False)
    free_user_upload = await _make_user(db_session, 980006, is_admin=False)

    # 1. Valid curated persona still works
    req_preset = api_mod.VoiceGenerateRequest(
        text="မင်္ဂလာပါ စမ်းသပ်မှု",
        engine="voxcpm",
        preset_voice="komin",
        voice_ref_id=None,
    )
    resp_preset = await api_mod.create_voice_job(req_preset, user=free_user_preset, db=db_session, _gate=free_user_preset)
    assert resp_preset["success"] is True
    job_preset = await db_session.get(Job, resp_preset["job_id"])
    assert job_preset is not None
    assert job_preset.preset_voice == "komin"

    # 2. Multipart upload flow create_voice_job_with_audio still works
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 16000)
    buf.seek(0)
    upload = UploadFile(filename="sample.wav", file=buf)
    resp_up = await api_mod.create_voice_job_with_audio(
        text="မင်္ဂလာပါ အသံဖိုင်",
        model_slug="voxcpm-cloud-ai",
        speed=api_mod.DEFAULT_SPEED,
        gender_preset="m2m",
        engine="voxcpm",
        preset_voice=None,
        reference_audio=upload,
        user=free_user_upload,
        db=db_session,
        _gate=free_user_upload,
    )
    assert resp_up["success"] is True
    job_up = await db_session.get(Job, resp_up["job_id"])
    assert job_up is not None
    assert job_up.source_audio_path and Path(job_up.source_audio_path).is_file()
