"""Round 7 regression pins (2026-11-21):

- Item 1: Busy generate-button label is solely `submit_already_creating`
  ("✓ ဖန်တီးနေပါပြီ" MY / "✓ Creating…" EN) with no settings hint and no
  appended minute-estimate badge while busy.
- Item 2: `#voice-inline-status` removed from Voice Studio (Recap & TTS
  inline strips untouched); Voice detail view (`#voice-progress-box`) and
  open `#submit-confirm` dialog finalize cleanly on terminal states
  (SUCCESS / FAILED / CANCELLED) via LiveJobs, hiding/disabling Cancel
  and stopping polling.
- Item 3: My Voice (custom) mode blocks generation when neither a reference
  audio file nor a saved voice is selected — enforced on BOTH client
  (`handleVoiceGenerate`, including admins) and server (`POST /api/voices/generate`,
  HTTP 400 with no Job row created), while curated persona, saved-voice
  (`voice_ref_id`), and `/api/voices/generate-with-audio` paths keep working.
"""
from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from starlette.datastructures import UploadFile

from app.config import settings
from app.models.enums import AccountCategory
from app.models.schema import Job
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")


def _translations() -> dict:
    proc = subprocess.run(
        [
            "node",
            "-e",
            "const fs=require('fs'),vm=require('vm');"
            "const c={window:{},localStorage:{getItem:()=>null,setItem:()=>{}}};"
            "vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8')+';this._T=translations;',c);"
            "console.log(JSON.stringify(c._T));",
            str(ROOT / "app/webapp/static/js/i18n.js"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(proc.stdout)


def _run_node(script: str) -> dict:
    proc = subprocess.run(
        ["node"],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── Item 1: Busy generate-button label ──────────────────────────────────────

def test_round7_item1_i18n_short_busy_label():
    tr = _translations()
    assert tr["my"]["submit_already_creating"] == "✓ ဖန်တီးနေပါပြီ"
    assert tr["en"]["submit_already_creating"] == "✓ Creating…"
    # No settings hint or time estimate remains in either locale.
    assert "ဆက်တင်" not in tr["my"]["submit_already_creating"]
    assert "change any setting" not in tr["en"]["submit_already_creating"].lower()
    assert "မိနစ်" not in tr["my"]["submit_already_creating"]
    assert "min" not in tr["en"]["submit_already_creating"].lower()


def test_round7_item1_busy_button_suppresses_estimate_badge_node():
    """Verify via Node that while the voice submit guard is busy, the rendered
    button label is ONLY t('submit_already_creating') with no '(~0.1 မိနစ်)'
    estimate appended, and re-arming restores the original label + estimate."""
    fn_submit_btn = APP_JS[
        APP_JS.index("function setSubmitButtonState(kind)"):
        APP_JS.index("function studioSubmitBegin(kind)")
    ]
    fn_excess = APP_JS[
        APP_JS.index("function syncVoiceExcessAndCost(len, max)"):
        APP_JS.index("window.syncVoiceExcessAndCost = syncVoiceExcessAndCost;")
    ]

    script = f"""
const vm = require('vm');
function mkEl(init = {{}}) {{
    const classes = new Set();
    return Object.assign({{
        innerText: '',
        textContent: '',
        disabled: false,
        dataset: {{}},
        attrs: {{}},
        _children: {{}},
        classList: {{
            add(c) {{ classes.add(c); }},
            remove(c) {{ classes.delete(c); }},
            toggle(c, on) {{ if (on === undefined) {{ if (classes.has(c)) classes.delete(c); else classes.add(c); }} else if (on) classes.add(c); else classes.delete(c); }},
            contains(c) {{ return classes.has(c); }},
        }},
        getAttribute(k) {{ return this.attrs[k] || null; }},
        querySelector(sel) {{ return this._children[sel] || null; }},
    }}, init);
}}
const spanMy = mkEl({{ innerText: '🚀 အသံ ထုတ်မည်', attrs: {{ 'data-i18n': 'btn_generate' }} }});
const badge = mkEl({{ textContent: '' }});
const btn = mkEl({{ _children: {{ '.btn-text': spanMy, '.vcp-btn-est-cost': badge }} }});
const els = {{ 'btn-generate-voice': btn, 'voice-btn-est-cost': badge }};
let lang = 'my';
const dict = {{
    my: {{ btn_generate: '🚀 အသံ ထုတ်မည်', submit_already_creating: '✓ ဖန်တီးနေပါပြီ', est_cost_min_badge: '(~{{min}} မိနစ်)' }},
    en: {{ btn_generate: '🚀 Generate Voice', submit_already_creating: '✓ Creating…', est_cost_min_badge: '(~{{min}} min)' }},
}};
const ctx = {{
    window: {{}},
    state: {{ speed: '+12%', user: {{ is_admin: false }} }},
    voiceSubmitGuard: {{ inFlight: false, submittedFingerprint: null }},
    recapSubmitGuard: {{ inFlight: false, submittedFingerprint: null }},
    ttsSubmitGuard: {{ inFlight: false, submittedFingerprint: null }},
    document: {{ getElementById: (id) => els[id] || null }},
    t: (k) => dict[lang][k] || k,
    tf: (k, v) => (dict[lang][k] || k).replace('{{min}}', v.min),
    estimateSpeechMinutesFromText: () => '0.1',
    recapJobActive: () => false,
    recapJobIsActive: () => false,
    refreshStickyCtas: () => {{}},
}};
vm.runInNewContext({json.dumps(fn_submit_btn + "\n" + fn_excess)}, ctx);

// 1. Pre-submit: estimate badge is shown next to generate label.
ctx.syncVoiceExcessAndCost(30, 1000);
const beforeLabel = (spanMy.innerText + badge.textContent).trim();
const beforeBadgeHidden = badge.classList.contains('hidden');

// 2. Busy (submitted): MY label is EXACTLY "✓ ဖန်တီးနေပါပြီ", badge cleared & hidden.
ctx.voiceSubmitGuard.submittedFingerprint = 'fp-1';
ctx.setSubmitButtonState('voice');
ctx.syncVoiceExcessAndCost(30, 1000); // even a preflight repaint keeps badge suppressed while busy
const busyMyLabel = (spanMy.innerText + badge.textContent).trim();
const busyBadgeHidden = badge.classList.contains('hidden');
const busyDisabled = btn.disabled;

// 3. Switch to EN while busy: EXACTLY "✓ Creating…", badge still suppressed.
lang = 'en';
ctx.setSubmitButtonState('voice');
const busyEnLabel = (spanMy.innerText + badge.textContent).trim();

// 4. Re-armed (setting changed or terminal): normal label + estimate restored.
ctx.voiceSubmitGuard.submittedFingerprint = null;
ctx.syncVoiceExcessAndCost(30, 1000);
ctx.setSubmitButtonState('voice');
const afterEnLabel = (spanMy.innerText + badge.textContent).trim();
const afterBadgeHidden = badge.classList.contains('hidden');

console.log(JSON.stringify({{
    beforeLabel, beforeBadgeHidden,
    busyMyLabel, busyBadgeHidden, busyDisabled,
    busyEnLabel, afterEnLabel, afterBadgeHidden,
}}));
"""
    out = _run_node(script)
    assert out["beforeLabel"] == "🚀 အသံ ထုတ်မည် (~0.1 မိနစ်)"
    assert out["beforeBadgeHidden"] is False
    assert out["busyMyLabel"] == "✓ ဖန်တီးနေပါပြီ"
    assert out["busyBadgeHidden"] is True
    assert out["busyDisabled"] is True
    assert out["busyEnLabel"] == "✓ Creating…"
    assert out["afterEnLabel"] == "🚀 Generate Voice (~0.1 min)"
    assert out["afterBadgeHidden"] is False


# ── Item 2: Remove voice inline strip + terminal-state sync ─────────────────

def test_round7_item2_voice_inline_status_removed_recap_kept():
    assert 'id="voice-inline-status"' not in INDEX_HTML
    assert 'id="voice-inline-status-text"' not in INDEX_HTML
    assert "#voice-inline-status" not in STYLE_CSS
    # Recap inline strip stays intact and untouched.
    assert 'id="recap-inline-status"' in INDEX_HTML
    assert 'id="recap-inline-status-text"' in INDEX_HTML

    voice_submit = APP_JS[
        APP_JS.index("async function handleVoiceGenerate()"):
        APP_JS.index("function startJobPolling")
    ]
    assert 'showStudioInlineStatus("voice"' not in voice_submit
    assert 'showStudioInlineStatus("recap"' in APP_JS

    # Shared updater ignores voice but still updates recap/tts.
    fn_inline = APP_JS[
        APP_JS.index("function showStudioInlineStatus(kind, text)"):
        APP_JS.index("window.showStudioInlineStatus = showStudioInlineStatus;")
    ]
    script = f"""
const vm = require('vm');
function mkEl() {{
    const s = new Set(['hidden']);
    return {{ innerText: '', classList: {{ remove(c){{ s.delete(c); }}, contains(c){{ return s.has(c); }} }} }};
}}
const els = {{
    'voice-inline-status': mkEl(), 'voice-inline-status-text': mkEl(),
    'recap-inline-status': mkEl(), 'recap-inline-status-text': mkEl(),
}};
const ctx = {{ document: {{ getElementById: (id) => els[id] || null }} }};
vm.runInNewContext({json.dumps(fn_inline)}, ctx);
ctx.showStudioInlineStatus('voice', 'should not render');
ctx.showStudioInlineStatus('recap', 'Recap stage 1');
console.log(JSON.stringify({{
    voiceHidden: els['voice-inline-status'].classList.contains('hidden'),
    voiceText: els['voice-inline-status-text'].innerText,
    recapHidden: els['recap-inline-status'].classList.contains('hidden'),
    recapText: els['recap-inline-status-text'].innerText,
}}));
"""
    out = _run_node(script)
    assert out["voiceHidden"] is True
    assert out["voiceText"] == ""
    assert out["recapHidden"] is False
    assert out["recapText"] == "Recap stage 1"


def test_round7_item2_voice_terminal_state_sync_detail_and_dialog_node():
    """Verify via Node that when a voice job transitions to SUCCESS, FAILED,
    or CANCELLED:
    - #voice-progress-box status text updates to the outcome (never stale RUNNING)
    - #btn-cancel-voice-job is hidden AND disabled
    - state.pollTimer is cleared
    - open #submit-confirm dialog finalizes via LiveJobs (no stale RUNNING)
    - openVoiceDetail() on a terminal job does NOT restart polling or unhide Cancel.
    """
    live_slice = APP_JS[
        APP_JS.index("const LIVE_JOB_TERMINAL_STATUSES"):
        APP_JS.index("window.LiveJobs = LiveJobs;") + len("window.LiveJobs = LiveJobs;")
    ]
    stage_slice = APP_JS[
        APP_JS.index("function liveJobStageText(job)"):
        APP_JS.index("function liveJobOverrunText(job)")
    ]
    paint_slice = APP_JS[
        APP_JS.index("function paintProgressBarFill(el, job)"):
        APP_JS.index("function syncRecapStateFromLiveJobs()")
    ]
    open_slice = APP_JS[
        APP_JS.index("function openVoiceDetail(jobId)"):
        APP_JS.index("function openRecapDetail(jobId)")
    ]
    confirm_and_sync_slice = APP_JS[
        APP_JS.index("state.submitConfirmJob = null;"):
        APP_JS.index("// History cards for tracked jobs")
    ]

    bundle = "\n".join([live_slice, stage_slice, paint_slice, open_slice, confirm_and_sync_slice])
    script = f"""
const vm = require('vm');
function mkEl(init = {{}}) {{
    const classes = new Set(init._classes || []);
    return Object.assign({{
        innerText: '',
        textContent: '',
        disabled: false,
        style: {{ width: '' }},
        dataset: {{}},
        _scrolled: false,
        classList: {{
            add(c) {{ classes.add(c); }},
            remove(c) {{ classes.delete(c); }},
            toggle(c, on) {{ if (on) classes.add(c); else classes.delete(c); }},
            contains(c) {{ return classes.has(c); }},
        }},
        scrollIntoView() {{ this._scrolled = true; }},
        setAttribute() {{}},
        getAttribute() {{ return null; }},
        querySelector() {{ return null; }},
    }}, init);
}}
const els = {{
    'tab-voice': mkEl({{ _classes: ['active'] }}),
    'voice-progress-box': mkEl(),
    'voice-result-box': mkEl({{ _classes: ['hidden'] }}),
    'voice-failed-box': mkEl({{ _classes: ['hidden'] }}),
    'voice-failed-msg': mkEl(),
    'voice-spinner': mkEl({{ innerText: '⚡', _classes: ['spinner-wave'] }}),
    'voice-status-msg': mkEl({{ innerText: '⚡ အသံ ဖန်တီးနေပါသည်…' }}),
    'voice-progress-fill': mkEl({{ _classes: ['indeterminate'] }}),
    'voice-eta-text': mkEl({{ innerText: '⏱️ 12s' }}),
    'btn-cancel-voice-job': mkEl({{ disabled: false }}),
    'submit-confirm': mkEl(),
    'submit-confirm-cost': mkEl(),
    'submit-confirm-stage': mkEl({{ innerText: 'RUNNING · ⚡ အသံ ဖန်တီးနေပါသည်…' }}),
    'submit-confirm-fill': mkEl({{ _classes: ['indeterminate'] }}),
}};
let clearedTimers = [];
let pollStarts = 0;
let fetchedUrls = [];
const state = {{
    activeJobId: 'vj_1',
    pollTimer: 42,
    voiceDetailOpen: true,
    submitConfirmJob: {{ kind: 'voice', id: 'vj_1' }},
    _voiceResultRenderedJobId: 'vj_1',
    _voiceTerminalHandledJobId: null,
}};
const dict = {{
    live_job_status_success: 'ပြီးပါပြီ',
    live_job_status_failed: 'မအောင်မြင်ပါ',
    live_job_status_cancelled: 'ရပ်ဆိုင်းလိုက်ပါပြီ',
    voice_synthesizing: '⚡ အသံ ဖန်တီးနေပါသည်…',
}};
const ctx = {{
    window: {{}},
    document: {{
        hidden: false,
        getElementById: (id) => els[id] || null,
        addEventListener: () => {{}},
    }},
    state,
    voiceSubmitGuard: {{ inFlight: false, submittedFingerprint: 'fp-1' }},
    clearInterval: (id) => clearedTimers.push(id),
    setTimeout: () => 1,
    clearTimeout: () => {{}},
    t: (k) => dict[k] || k,
    tf: (k) => dict[k] || k,
    getAuthHeaders: () => ({{}}),
    fetch: async (url) => {{
        fetchedUrls.push(url);
        return {{ ok: true, json: async () => ({{ status: 'SUCCESS', output_audio_url: '/api/audio/download/vj_1' }}) }};
    }},
    setVoiceStage: () => {{}},
    setSubmitButtonState: () => {{}},
    renderLiveJobsPill: () => {{}},
    renderLiveJobsSheet: () => {{}},
    applyLiveJobToHistory: () => {{}},
    syncRecapStateFromLiveJobs: () => {{}},
    switchTab: () => {{}},
    syncTelegramBackButton: () => {{}},
    startJobPolling: () => {{ pollStarts++; }},
    openRecapDetail: () => {{}},
    loadUserProfile: () => {{}},
    loadUserHistory: () => {{}},
    mapErrorToToast: (m) => m || 'err',
}};
vm.runInNewContext({json.dumps(bundle)}, ctx);

// Start as RUNNING then transition to SUCCESS via LiveJobs.
state.submitConfirmJob = {{ kind: 'voice', id: 'vj_1' }};
ctx.window.LiveJobs.track({{ id: 'vj_1', kind: 'voice', status: 'RUNNING', progress_percent: 40 }});
els['voice-result-box'].classList.remove('hidden');
ctx.window.LiveJobs.track({{ id: 'vj_1', kind: 'voice', status: 'SUCCESS', progress_percent: 100, stage_index: 1, stage_total: 1 }});

const afterSuccess = {{
    timerCleared: clearedTimers.includes(42) && state.pollTimer === null,
    statusMsg: els['voice-status-msg'].innerText,
    spinner: els['voice-spinner'].innerText,
    etaCleared: els['voice-eta-text'].innerText === '',
    cancelHidden: els['btn-cancel-voice-job'].classList.contains('hidden'),
    cancelDisabled: els['btn-cancel-voice-job'].disabled,
    barWidth: els['voice-progress-fill'].style.width,
    barIndet: els['voice-progress-fill'].classList.contains('indeterminate'),
    dialogStage: els['submit-confirm-stage'].innerText,
    dialogBarWidth: els['submit-confirm-fill'].style.width,
    guardCleared: ctx.voiceSubmitGuard.submittedFingerprint === null,
    progressBoxHidden: els['voice-progress-box'].classList.contains('hidden'),
}};

// Calling openVoiceDetail('vj_1') after completion must NOT restart polling or unhide progress/Cancel.
ctx.openVoiceDetail('vj_1');
const afterReopenSuccess = {{
    pollStarts,
    progressBoxHidden: els['voice-progress-box'].classList.contains('hidden'),
    cancelHidden: els['btn-cancel-voice-job'].classList.contains('hidden'),
    cancelDisabled: els['btn-cancel-voice-job'].disabled,
    resultScrolled: els['voice-result-box']._scrolled,
}};

// Also verify CANCELLED terminal state via openVoiceDetail.
state.activeJobId = 'vj_2';
state.pollTimer = 99;
state._voiceTerminalHandledJobId = null;
state.submitConfirmJob = {{ kind: 'voice', id: 'vj_2' }};
ctx.window.LiveJobs.track({{ id: 'vj_2', kind: 'voice', status: 'CANCELLED', progress_percent: 0 }});
ctx.openVoiceDetail('vj_2');
const afterCancelled = {{
    timerCleared: clearedTimers.includes(99) && state.pollTimer === null,
    pollStarts,
    statusMsg: els['voice-status-msg'].innerText,
    cancelHidden: els['btn-cancel-voice-job'].classList.contains('hidden'),
    cancelDisabled: els['btn-cancel-voice-job'].disabled,
    dialogStage: els['submit-confirm-stage'].innerText,
}};

console.log(JSON.stringify({{ afterSuccess, afterReopenSuccess, afterCancelled }}));
"""
    out = _run_node(script)
    s = out["afterSuccess"]
    assert s["timerCleared"] is True
    assert s["statusMsg"] == "ပြီးပါပြီ"
    assert s["spinner"] == "✅"
    assert s["etaCleared"] is True
    assert s["cancelHidden"] is True
    assert s["cancelDisabled"] is True
    assert s["barWidth"] == "100%"
    assert s["barIndet"] is False
    assert s["dialogStage"] == "ပြီးပါပြီ"
    assert s["dialogBarWidth"] == "100%"
    assert s["guardCleared"] is True
    assert s["progressBoxHidden"] is True

    r = out["afterReopenSuccess"]
    assert r["pollStarts"] == 0
    assert r["progressBoxHidden"] is True
    assert r["cancelHidden"] is True
    assert r["cancelDisabled"] is True
    assert r["resultScrolled"] is True

    c = out["afterCancelled"]
    assert c["timerCleared"] is True
    assert c["pollStarts"] == 0
    assert c["statusMsg"] == "ရပ်ဆိုင်းလိုက်ပါပြီ"
    assert c["cancelHidden"] is True
    assert c["cancelDisabled"] is True
    assert c["dialogStage"] == "ရပ်ဆိုင်းလိုက်ပါပြီ"


def test_round7_item2_start_job_polling_pushes_terminal_to_livejobs_and_stops_node():
    """Verify via Node that startJobPolling():
    - Immediately polls (0ms initial tick) and pushes RUNNING -> SUCCESS to LiveJobs
    - Finalizes #submit-confirm and #voice-progress-box (no stale RUNNING, Cancel hidden+disabled)
    - Stops polling and refuses to restart if invoked again for an already-terminal job.
    """
    live_slice = APP_JS[
        APP_JS.index("const LIVE_JOB_TERMINAL_STATUSES"):
        APP_JS.index("window.LiveJobs = LiveJobs;") + len("window.LiveJobs = LiveJobs;")
    ]
    stage_slice = APP_JS[
        APP_JS.index("function liveJobStageText(job)"):
        APP_JS.index("function liveJobOverrunText(job)")
    ]
    paint_slice = APP_JS[
        APP_JS.index("function paintProgressBarFill(el, job)"):
        APP_JS.index("function syncRecapStateFromLiveJobs()")
    ]
    confirm_and_sync_slice = APP_JS[
        APP_JS.index("state.submitConfirmJob = null;"):
        APP_JS.index("// History cards for tracked jobs")
    ]
    poll_slice = APP_JS[
        APP_JS.index("function renderVoiceFallbackNote(data)"):
        APP_JS.index("window.cancelVoiceJob = function()")
    ]
    bundle = "\n".join([live_slice, stage_slice, paint_slice, confirm_and_sync_slice, poll_slice])

    script = f"""
const vm = require('vm');
function mkEl(init = {{}}) {{
    const classes = new Set(init._classes || []);
    return Object.assign({{
        innerText: '',
        textContent: '',
        innerHTML: '',
        disabled: false,
        style: {{ width: '' }},
        dataset: {{}},
        attrs: {{}},
        classList: {{
            add(c) {{ classes.add(c); }},
            remove(c) {{ classes.delete(c); }},
            toggle(c, on) {{ if (on) classes.add(c); else classes.delete(c); }},
            contains(c) {{ return classes.has(c); }},
        }},
        setAttribute(k, v) {{ this.attrs[k] = v; }},
        getAttribute(k) {{ return this.attrs[k] || null; }},
        querySelector() {{ return null; }},
        load() {{}},
        play() {{ return Promise.resolve(); }},
        scrollIntoView() {{}},
    }}, init);
}}
const els = {{
    'voice-progress-box': mkEl(),
    'voice-result-box': mkEl({{ _classes: ['hidden'] }}),
    'voice-failed-box': mkEl({{ _classes: ['hidden'] }}),
    'voice-failed-msg': mkEl(),
    'voice-spinner': mkEl({{ innerText: '⚡', _classes: ['spinner-wave'] }}),
    'voice-status-msg': mkEl(),
    'voice-progress-fill': mkEl(),
    'voice-eta-text': mkEl(),
    'btn-cancel-voice-job': mkEl({{ _classes: ['hidden'], disabled: true }}),
    'btn-generate-voice': mkEl({{ disabled: true }}),
    'btn-voice-send-telegram': mkEl(),
    'btn-download-audio': mkEl(),
    'result-audio-player': mkEl(),
    'voice-fallback-note': mkEl({{ _classes: ['hidden'] }}),
    'submit-confirm-stage': mkEl({{ innerText: 'QUEUED' }}),
    'submit-confirm-fill': mkEl(),
}};
let statusPayloads = [
    {{ status: 'RUNNING', progress_percent: 30 }},
    {{ status: 'SUCCESS', output_audio_url: '/api/audio/download/vj_poll_1', telegram_sendable: true }},
];
let fetchCount = 0;
let intervalCb = null;
let intervalCleared = false;
const state = {{ activeJobId: null, pollTimer: null, voiceDetailOpen: true }};
const dict = {{
    live_job_status_success: 'Done',
    live_job_status_failed: 'Failed',
    live_job_status_cancelled: 'Cancelled',
    voice_synthesizing: '⚡ Generating your voice…',
    voice_elapsed: '⏱️ {{sec}}s',
    btn_send_voice_telegram: 'Send to Telegram',
    alert_voice_ready: 'Ready',
    alert_voice_ready_body1: 'Audio ready',
    alert_btn_listen: 'Listen',
}};
const ctx = {{
    window: {{}},
    document: {{ hidden: false, getElementById: (id) => els[id] || null, addEventListener: () => {{}} }},
    state,
    voiceSubmitGuard: {{ inFlight: false, submittedFingerprint: 'fp-poll' }},
    setInterval: (fn) => {{ intervalCb = fn; return 77; }},
    clearInterval: (id) => {{ if (id === 77) intervalCleared = true; }},
    setTimeout: () => 1,
    clearTimeout: () => {{}},
    t: (k) => dict[k] || k,
    tf: (k, v) => (dict[k] || k).replace('{{sec}}', v && v.sec != null ? v.sec : 0),
    getAuthHeaders: () => ({{}}),
    fetch: async () => {{
        fetchCount++;
        const payload = statusPayloads.shift() || {{ status: 'SUCCESS' }};
        return {{ ok: true, json: async () => payload }};
    }},
    setVoiceStage: () => {{}},
    setSubmitButtonState: () => {{}},
    renderLiveJobsPill: () => {{}},
    renderLiveJobsSheet: () => {{}},
    applyLiveJobToHistory: () => {{}},
    syncRecapStateFromLiveJobs: () => {{}},
    applyTelegramSendable: () => {{}},
    setAuthMediaSource: async () => true,
    brandedDlName: () => 'Voice.wav',
    haptic: () => {{}},
    loadUserProfile: () => {{}},
    loadUserHistory: () => {{}},
    showFloatingAlert: () => {{}},
    showStudioToast: () => {{}},
    mapErrorToToast: (m) => m || 'err',
    setCustomCloneStep: () => {{}},
}};
vm.runInNewContext({json.dumps(bundle)}, ctx);

(async () => {{
    state.submitConfirmJob = {{ kind: 'voice', id: 'vj_poll_1' }};
    ctx.window.LiveJobs.track({{ id: 'vj_poll_1', kind: 'voice', status: 'QUEUED', progress_percent: 0 }});
    ctx.startJobPolling('vj_poll_1');
    await new Promise(r => setImmediate(r));
    const whileRunning = {{
        fetchCount,
        statusMsg: els['voice-status-msg'].innerText,
        cancelHidden: els['btn-cancel-voice-job'].classList.contains('hidden'),
        cancelDisabled: els['btn-cancel-voice-job'].disabled,
        dialogStage: els['submit-confirm-stage'].innerText,
    }};
    // Next tick returns SUCCESS.
    await intervalCb();
    const afterSuccess = {{
        fetchCount,
        intervalCleared,
        pollTimerNull: state.pollTimer === null,
        statusMsg: els['voice-status-msg'].innerText,
        cancelHidden: els['btn-cancel-voice-job'].classList.contains('hidden'),
        cancelDisabled: els['btn-cancel-voice-job'].disabled,
        progressHidden: els['voice-progress-box'].classList.contains('hidden'),
        resultHidden: els['voice-result-box'].classList.contains('hidden'),
        dialogStage: els['submit-confirm-stage'].innerText,
        liveStatus: ctx.window.LiveJobs.jobs.get('vj_poll_1').status,
    }};
    // Calling startJobPolling again on the completed job must be a no-op (no new fetch, Cancel stays hidden).
    const beforeSecond = fetchCount;
    ctx.startJobPolling('vj_poll_1');
    await new Promise(r => setImmediate(r));
    const afterSecondCall = {{
        newFetches: fetchCount - beforeSecond,
        pollTimerNull: state.pollTimer === null,
        cancelHidden: els['btn-cancel-voice-job'].classList.contains('hidden'),
        cancelDisabled: els['btn-cancel-voice-job'].disabled,
    }};
    console.log(JSON.stringify({{ whileRunning, afterSuccess, afterSecondCall }}));
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
    out = _run_node(script)
    assert out["whileRunning"]["fetchCount"] == 1
    assert out["whileRunning"]["statusMsg"] == "⚡ Generating your voice…"
    assert out["whileRunning"]["cancelHidden"] is False
    assert out["whileRunning"]["cancelDisabled"] is False
    assert "RUNNING" in out["whileRunning"]["dialogStage"]

    assert out["afterSuccess"]["intervalCleared"] is True
    assert out["afterSuccess"]["pollTimerNull"] is True
    assert out["afterSuccess"]["statusMsg"] == "Done"
    assert out["afterSuccess"]["cancelHidden"] is True
    assert out["afterSuccess"]["cancelDisabled"] is True
    assert out["afterSuccess"]["progressHidden"] is True
    assert out["afterSuccess"]["resultHidden"] is False
    assert out["afterSuccess"]["dialogStage"] == "Done"
    assert out["afterSuccess"]["liveStatus"] == "SUCCESS"

    assert out["afterSecondCall"]["newFetches"] == 0
    assert out["afterSecondCall"]["pollTimerNull"] is True
    assert out["afterSecondCall"]["cancelHidden"] is True
    assert out["afterSecondCall"]["cancelDisabled"] is True


# ── Item 3: My Voice mode must not generate without a voice sample ──────────

def test_round7_item3_i18n_and_server_localization():
    tr = _translations()
    assert tr["my"]["toast_custom_voice_needs_sample"] == (
        "အသံနမူနာ အရင် ရွေးချယ်ပါ — သို့မဟုတ် သိမ်းထားတဲ့ အသံကို ရွေးပါ"
    )
    assert tr["en"]["toast_custom_voice_needs_sample"] == (
        "Choose a voice sample first — or select a saved voice"
    )

    # Verify localizeServerText translates the MY 400 detail in EN mode.
    script = f"""
const fs = require('fs'), vm = require('vm');
const c = {{ window: {{}}, localStorage: {{ getItem: () => 'en', setItem: () => {{}} }} }};
vm.runInNewContext(fs.readFileSync({json.dumps(str(ROOT / "app/webapp/static/js/i18n.js"))}, 'utf8'), c);
const outEn = c.localizeServerText('အသံနမူနာ အရင် ရွေးချယ်ပါ — သို့မဟုတ် သိမ်းထားတဲ့ အသံကို ရွေးပါ');
console.log(JSON.stringify({{ outEn }}));
"""
    out = _run_node(script)
    assert out["outEn"] == "Choose a voice sample first — or select a saved voice"


def test_round7_item3_client_guard_blocks_custom_without_sample_node():
    """Verify via Node that handleVoiceGenerate() in custom mode with NO
    reference file and NO saved custom voice aborts before any fetch (and
    before any cost confirmation) and shows toast_custom_voice_needs_sample —
    for BOTH regular users AND admins — while persona mode and saved-voice
    mode proceed normally."""
    fn_handle = APP_JS[
        APP_JS.index("async function handleVoiceGenerate()"):
        APP_JS.index("function renderVoiceFallbackNote(data)")
    ]

    script = f"""
const vm = require('vm');
function mkEl(init = {{}}) {{
    const s = new Set();
    return Object.assign({{
        value: '',
        innerText: '',
        disabled: false,
        classList: {{ add(c){{s.add(c);}}, remove(c){{s.delete(c);}}, toggle(c,on){{ if(on)s.add(c); else s.delete(c); }} }},
    }}, init);
}}
const els = {{
    'voice-text-input': mkEl({{ value: 'မင်္ဂလာပါ စမ်းသပ်ချက်' }}),
    'voice-progress-box': mkEl(),
    'voice-result-box': mkEl(),
    'voice-status-msg': mkEl(),
    'voice-progress-fill': mkEl(),
    'voice-eta-text': mkEl(),
    'btn-generate-voice': mkEl(),
    'voice-spinner': mkEl(),
    'btn-cancel-voice-job': mkEl(),
}};
let fetchCalls = [];
let toasts = [];
let confirms = [];
let failures = 0;
let successes = 0;
const state = {{
    voxcpmSubMode: 'custom',
    selectedReferenceFile: null,
    savedCustomVoiceActiveId: '',
    selectedPersona: 'komin',
    speed: '+12%',
    user: {{ is_admin: true, is_vip: true, character_limit: 5000 }},
    _voiceCostConfirmed: false,
}};
const ctx = {{
    window: {{}},
    state,
    document: {{ getElementById: (id) => els[id] || null }},
    LiveJobs: {{ hasActiveJob: () => false, track: () => {{}} }},
    studioSubmitBegin: () => true,
    studioSubmitFailure: () => {{ failures++; }},
    studioSubmitSuccess: () => {{ successes++; }},
    showLiveSubmitLimit: () => {{}},
    showStudioToast: (msg, type) => toasts.push({{ msg, type }}),
    showStudioConfirm: (msg) => confirms.push(msg),
    showErrorToast: (e) => toasts.push({{ msg: String(e), type: 'error' }}),
    t: (k) => k === 'toast_custom_voice_needs_sample'
        ? 'အသံနမူနာ အရင် ရွေးချယ်ပါ — သို့မဟုတ် သိမ်းထားတဲ့ အသံကို ရွေးပါ'
        : k,
    tf: (k) => k,
    haptic: () => {{}},
    setVoiceStage: () => {{}},
    paintProgressBarFill: () => {{}},
    startJobPolling: () => {{}},
    showSubmitConfirmation: () => {{}},
    getAuthHeaders: () => ({{}}),
    fetchWithTimeout: async (url, opts) => {{
        fetchCalls.push({{ url, body: opts && opts.body }});
        return {{ ok: true, json: async () => ({{ success: true, job_id: 'vj_ok' }}) }};
    }},
}};
vm.runInNewContext({json.dumps(fn_handle)}, ctx);

(async () => {{
    // Case A: ADMIN in custom mode with NO file and NO saved voice -> BLOCKED.
    await ctx.handleVoiceGenerate();
    const adminBlocked = {{
        fetchCount: fetchCalls.length,
        confirmCount: confirms.length,
        toast: toasts[toasts.length - 1],
        failures,
    }};

    // Case B: Non-admin user in custom mode with NO file and NO saved voice -> BLOCKED before cost confirm!
    state.user = {{ is_admin: false, is_vip: false, character_limit: 1000 }};
    await ctx.handleVoiceGenerate();
    const userBlocked = {{
        fetchCount: fetchCalls.length,
        confirmCount: confirms.length,
        toast: toasts[toasts.length - 1],
        failures,
    }};

    // Case C: Custom mode WITH savedCustomVoiceActiveId -> proceeds to /api/voices/generate with voice_ref_id.
    state.user = {{ is_admin: true, is_vip: true, character_limit: 5000 }};
    state.savedCustomVoiceActiveId = 'webapp_ref_u1_abcdef012345';
    await ctx.handleVoiceGenerate();
    const savedCall = fetchCalls[fetchCalls.length - 1];

    // Case D: Curated (persona) mode with no file/saved voice -> proceeds with preset_voice.
    state.voxcpmSubMode = 'curated';
    state.savedCustomVoiceActiveId = '';
    state.selectedPersona = 'komin';
    await ctx.handleVoiceGenerate();
    const personaCall = fetchCalls[fetchCalls.length - 1];

    console.log(JSON.stringify({{
        adminBlocked,
        userBlocked,
        savedUrl: savedCall && savedCall.url,
        savedBody: savedCall ? JSON.parse(savedCall.body) : null,
        personaUrl: personaCall && personaCall.url,
        personaBody: personaCall ? JSON.parse(personaCall.body) : null,
    }}));
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
    out = _run_node(script)
    expected_toast = "အသံနမူနာ အရင် ရွေးချယ်ပါ — သို့မဟုတ် သိမ်းထားတဲ့ အသံကို ရွေးပါ"
    assert out["adminBlocked"]["fetchCount"] == 0
    assert out["adminBlocked"]["confirmCount"] == 0
    assert out["adminBlocked"]["toast"] == {"msg": expected_toast, "type": "warning"}
    assert out["adminBlocked"]["failures"] == 1

    assert out["userBlocked"]["fetchCount"] == 0
    assert out["userBlocked"]["confirmCount"] == 0
    assert out["userBlocked"]["toast"] == {"msg": expected_toast, "type": "warning"}
    assert out["userBlocked"]["failures"] == 2

    assert out["savedUrl"] == "/api/voices/generate"
    assert out["savedBody"]["voice_ref_id"] == "webapp_ref_u1_abcdef012345"
    assert out["savedBody"]["preset_voice"] is None

    assert out["personaUrl"] == "/api/voices/generate"
    assert out["personaBody"]["preset_voice"] == "komin"
    assert out["personaBody"]["voice_ref_id"] is None


@pytest.mark.asyncio
async def test_round7_item3_api_rejects_voxcpm_without_preset_or_voice_ref(
    db_session, monkeypatch
):
    """POST /api/voices/generate with engine=voxcpm, preset_voice=None, and
    voice_ref_id=None returns HTTP 400 and creates NO Job row — for both
    regular users and admins."""
    regular, _ = await get_or_create_user(db_session, telegram_id=997001)
    regular.account_category = AccountCategory.PURCHASED.value
    regular.balance_seconds = 600
    admin, _ = await get_or_create_user(db_session, telegram_id=997002)
    await db_session.commit()
    monkeypatch.setattr(settings, "admin_ids_raw", str(admin.id))

    req = api_mod.VoiceGenerateRequest(
        text="မင်္ဂလာပါ စမ်းသပ်ချက်",
        engine="voxcpm",
        preset_voice=None,
        voice_ref_id=None,
    )
    for actor in (regular, admin):
        with pytest.raises(HTTPException) as exc:
            await api_mod.create_voice_job(req, user=actor, db=db_session, _gate=actor)
        assert exc.value.status_code == 400
        assert "အသံနမူနာ" in str(exc.value.detail)

    # Also reject when voice_ref_id points to a missing / unresolvable sample.
    bad_ref_req = api_mod.VoiceGenerateRequest(
        text="မင်္ဂလာပါ စမ်းသပ်ချက်",
        engine="voxcpm",
        preset_voice=None,
        voice_ref_id=f"webapp_ref_u{admin.id}_000000000000",
    )
    with pytest.raises(HTTPException) as exc:
        await api_mod.create_voice_job(bad_ref_req, user=admin, db=db_session, _gate=admin)
    assert exc.value.status_code == 400

    # Zero Job rows were created across all rejected attempts.
    count = (await db_session.execute(select(func.count()).select_from(Job))).scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_round7_item3_api_allows_valid_preset_voice(db_session):
    """POST /api/voices/generate with a valid curated preset_voice still succeeds."""
    user, _ = await get_or_create_user(db_session, telegram_id=997003)
    user.account_category = AccountCategory.PURCHASED.value
    user.balance_seconds = 600
    await db_session.commit()

    req = api_mod.VoiceGenerateRequest(
        text="မင်္ဂလာပါ",
        engine="voxcpm",
        preset_voice="komin",
        voice_ref_id=None,
    )
    resp = await api_mod.create_voice_job(req, user=user, db=db_session, _gate=user)
    assert resp["success"] is True
    assert resp["preset_voice"] == "komin"
    job = await db_session.get(Job, resp["job_id"])
    assert job is not None
    assert job.preset_voice == "komin"


@pytest.mark.asyncio
async def test_round7_item3_api_allows_valid_saved_voice_ref_id(
    db_session, tmp_path, monkeypatch
):
    """POST /api/voices/generate with preset_voice=None and a valid resolvable
    voice_ref_id still succeeds and stores source_audio_path."""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    user, _ = await get_or_create_user(db_session, telegram_id=997004)
    user.account_category = AccountCategory.PURCHASED.value
    user.balance_seconds = 600
    await db_session.commit()

    ref_dir = tmp_path / "temp" / "voxcpm_refs"
    ref_dir.mkdir(parents=True, exist_ok=True)
    ref_id = f"webapp_ref_u{user.id}_a1b2c3d4e5f6"
    sample_file = ref_dir / f"{ref_id}.wav"
    sample_file.write_bytes(b"RIFF" + b"\x00" * 512)

    req = api_mod.VoiceGenerateRequest(
        text="မင်္ဂလာပါ ကိုယ်ပိုင်အသံ",
        engine="voxcpm",
        preset_voice=None,
        voice_ref_id=ref_id,
    )
    resp = await api_mod.create_voice_job(req, user=user, db=db_session, _gate=user)
    assert resp["success"] is True
    assert resp["preset_voice"] is None
    job = await db_session.get(Job, resp["job_id"])
    assert job is not None
    assert job.source_audio_path == str(sample_file)


@pytest.mark.asyncio
async def test_round7_item3_api_generate_with_audio_still_succeeds(
    db_session, tmp_path, monkeypatch
):
    """POST /api/voices/generate-with-audio with an uploaded sample still works."""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(api_mod, "DISK_HEADROOM_MIN_FREE_BYTES", 0)
    user, _ = await get_or_create_user(db_session, telegram_id=997005)
    user.account_category = AccountCategory.PURCHASED.value
    user.balance_seconds = 600
    await db_session.commit()

    upload = UploadFile(filename="sample.wav", file=io.BytesIO(b"RIFF" + b"\x00" * 512))
    resp = await api_mod.create_voice_job_with_audio(
        text="မင်္ဂလာပါ ဖိုင်တင်အသံ",
        model_slug="voxcpm-cloud-ai",
        speed="+12%",
        gender_preset="m2m",
        engine="voxcpm",
        preset_voice=None,
        reference_audio=upload,
        user=user,
        db=db_session,
        _gate=user,
    )
    assert resp["success"] is True
    assert resp["has_reference_audio"] is True
    job = await db_session.get(Job, resp["job_id"])
    assert job is not None
    assert job.source_audio_path is not None
