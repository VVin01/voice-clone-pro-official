"""Regression suite for Recap UX Walk Findings (2026-11-15)."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.models.schema import User
from app.services.recap_engine import core
from app.webapp import api as api_mod

REPO = Path(__file__).resolve().parents[2]
INDEX_HTML = (REPO / "app/webapp/static/index.html").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "app/webapp/static/css/style.css").read_text(encoding="utf-8")
APP_JS = (REPO / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")


def _extract_fn(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.index(marker)
    i = src.index("{", start)
    depth = 0
    in_str = None
    in_regex = False
    in_line_comment = False
    in_block_comment = False
    in_regex_class = False
    escaped = False
    prev_sig = "{"
    while i < len(src):
        ch = src[i]
        nxt = src[i + 1] if i + 1 < len(src) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
        elif in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 1
        elif in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = None
                prev_sig = '"'
        elif in_regex:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "[":
                in_regex_class = True
            elif ch == "]" and in_regex_class:
                in_regex_class = False
            elif ch == "/" and not in_regex_class:
                in_regex = False
                prev_sig = ")"
        else:
            if ch == "/" and nxt == "/":
                in_line_comment = True
                i += 1
            elif ch == "/" and nxt == "*":
                in_block_comment = True
                i += 1
            elif ch == "/" and prev_sig in ("(", ",", "=", ":", "[", "!", "&", "|", "?", "{", ";", "\n", "n"):
                in_regex = True
            elif ch in ("'", '"', "`"):
                in_str = ch
            elif ch == "{":
                depth += 1
                prev_sig = ch
            elif ch == "}":
                depth -= 1
                prev_sig = ch
                if depth == 0:
                    return src[start : i + 1]
            elif not ch.isspace():
                prev_sig = ch
        i += 1
    raise AssertionError(f"Unbalanced braces for function {name}")


def _run_node(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, f"Node failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── Item 1: Step-2 preview subtitles & viewport clearance ───────────────────


def _locked_defaults_js() -> str:
    return "\n".join(
        [line for line in APP_JS.splitlines() if line.startswith("const RECAP_LOCKED_")]
        + [_extract_fn(APP_JS, "recapLockedCropForRatio")]
    )


def test_item1_step2_preview_composite_wiring_and_fit_via_node():
    fns = "\n".join(
        [
            _locked_defaults_js(),
            _extract_fn(APP_JS, "ddConfigExtras"),
            _extract_fn(APP_JS, "recapCompositeSourceUrl"),
            _extract_fn(APP_JS, "recapCompositePayload"),
            _extract_fn(APP_JS, "ddCompositePayload"),
            _extract_fn(APP_JS, "scheduleRecapCompositeRefresh"),
            _extract_fn(APP_JS, "showRecapStep"),
            _extract_fn(APP_JS, "ddFitPreviewFrame"),
        ]
    )
    script = f"""
const vm = require('vm');
const timers = [];
const els = {{}};
const mkEl = (id, init={{}}) => {{
  const obj = Object.assign({{
    id, value: '', style: {{}}, parentElement: {{ clientWidth: 320 }},
    classList: {{ _s: new Set(), contains(c){{return this._s.has(c)}}, add(c){{this._s.add(c)}}, remove(c){{this._s.delete(c)}}, toggle(c,f){{if(f===undefined)f=!this._s.has(c); f?this._s.add(c):this._s.delete(c);}} }},
    setAttribute(){{}}, removeAttribute(){{}}, scrollIntoView(){{}}, offsetWidth: 10,
  }}, init);
  els[id] = obj;
  return obj;
}};
mkEl('recap-url-input', {{ value: 'Check https://example.com/watch?v=1 please' }});
mkEl('recap-step-1'); mkEl('recap-step-2'); mkEl('recap-step-3'); mkEl('recap-step-4');
const frame = mkEl('recap-mockup-frame', {{ parentElement: {{ clientWidth: 300 }} }});

let scheduledCount = 0;
let summaryCount = 0;
const ctx = {{
  document: {{
    getElementById: (id) => els[id] || null,
    querySelector: () => null,
    querySelectorAll: () => [],
  }},
  window: {{ innerHeight: 720, set __ddFitPreviewFrame(fn){{}}, get __ddFitPreviewFrame(){{ return ctx.ddFitPreviewFrame; }} }},
  state: {{
    uploadedVideoPath: null,
    recapVerifiedSourceUrl: null,
    recapSourceDuration: 30,
    recapSeekSec: 5,
    recapRatio: '9:16',
    recapRatioFill: 'blur',
    recapBurnSubs: true,
    recapSubStyle: 'yellow',
    recapFontSize: 13,
    recapLines: 1,
    recapBlurBoxes: [],
  }},
  recapCurrentStep: 1,
  recapCompositeTimer: null,
  refreshRecapCompositePreview: () => {{}},
  recapNormalMaxSeconds: () => 300,
  recapDurationGateState: () => 'ok',
  recapFirstIncompleteBefore: () => null,
  recapStepEls: (m) => ({{ card: els['recap-step-' + m] }}),
  refreshRecapStepIndicator: () => {{}},
  refreshStickyCtas: () => {{}},
  syncTelegramBackButton: () => {{}},
  requestAnimationFrame: (fn) => fn(),
  setTimeout: (fn, ms) => {{ timers.push({{ fn, ms }}); return timers.length; }},
  clearTimeout: () => {{}},
  parseInt, parseFloat, Math, Array, Number, JSON,
}};
ctx.window.scheduleRecapCompositeRefresh = () => {{ scheduledCount += 1; }};
ctx.window.refreshRecapStep2Summary = () => {{ summaryCount += 1; }};
vm.runInNewContext({json.dumps(fns)}, ctx);

const rawUrl = ctx.recapCompositeSourceUrl();
ctx.state.recapVerifiedSourceUrl = 'https://cdn.example.com/v.mp4';
const verifiedUrl = ctx.recapCompositeSourceUrl();
const pGold13 = ctx.ddCompositePayload();
ctx.state.recapSubStyle = 'cinematic';
ctx.state.recapFontSize = 22;
const pCine22 = ctx.ddCompositePayload();
ctx.state.recapBurnSubs = false;
const pOff = ctx.ddCompositePayload();
ctx.state.uploadedVideoPath = 'up/test.mp4';
const uploadSrcUrl = ctx.recapCompositeSourceUrl();

ctx.showRecapStep(2);
ctx.ddFitPreviewFrame();
const h720 = parseInt(frame.style.height, 10);
ctx.window.innerHeight = 840;
ctx.ddFitPreviewFrame();
const h840 = parseInt(frame.style.height, 10);

console.log(JSON.stringify({{
  rawUrl, verifiedUrl, uploadSrcUrl,
  pGold13, pCine22, pOff,
  scheduledCount, summaryCount,
  h720, h840,
}}));
"""
    res = _run_node(script)
    assert res["rawUrl"] == "https://example.com/watch?v=1"
    assert res["verifiedUrl"] == "https://cdn.example.com/v.mp4"
    assert res["uploadSrcUrl"] is None
    assert res["pGold13"]["burn_subtitles"] is True
    assert res["pGold13"]["sub_style"] == "yellow"
    assert res["pGold13"]["sub_font_size"] == 13
    assert res["pCine22"]["burn_subtitles"] is True
    assert res["pCine22"]["sub_style"] == "cinematic"
    assert res["pCine22"]["sub_font_size"] == 22
    assert res["pGold13"] != res["pCine22"]
    assert res["pOff"]["burn_subtitles"] is False
    assert res["scheduledCount"] >= 1
    assert res["summaryCount"] >= 1
    assert res["h720"] <= 720 - 240
    assert res["h840"] <= 840 - 240
    assert "scheduleRecapCompositeRefresh();" in APP_JS
    assert 'btnToggleSubs.addEventListener("click"' in APP_JS
    assert "#tab-recap .vcp-step2-sticky-preview.is-ratio-9-16" in STYLE_CSS
    assert "position: relative;\n        top: auto;" in STYLE_CSS


def test_item1_preview_composite_forwards_style_and_size_to_png_renderer(tmp_path):
    """Verify the backend preview-composite path and shared PNG renderer config diverge for style/size."""
    assert core.subtitle_font_px(1080, 1920, 13, 1, None) < core.subtitle_font_px(1080, 1920, 22, 1, None)
    v_gold = core.build_subtitle_visuals(core.SUBTITLE_STYLE_PRESETS["yellow"], None, 1920, 48)
    v_cine = core.build_subtitle_visuals(core.SUBTITLE_STYLE_PRESETS["cinematic"], None, 1920, 64)
    assert v_gold["fill"] != v_cine["fill"]
    assert v_gold != v_cine

    up_dir = tmp_path / "temp" / "recap_uploads"
    up_dir.mkdir(parents=True, exist_ok=True)
    src = up_dir / "up.mp4"
    src.write_bytes(b"\x00" * 128)
    user = SimpleNamespace(id=1, account_category="PURCHASED", language_code="my")
    calls = []

    def fake_preview(source_path, **kwargs):
        calls.append(kwargs)
        out = tmp_path / f"out_{len(calls)}.jpg"
        out.write_bytes(b"JPEG")
        return str(out)

    req_gold = api_mod.RecapPreviewCompositeRequest(
        uploaded_path=str(src), sub_style="yellow", sub_font_size=13, burn_subtitles=True
    )
    req_cine = api_mod.RecapPreviewCompositeRequest(
        uploaded_path=str(src), sub_style="cinematic", sub_font_size=22, burn_subtitles=True
    )
    fake_settings = SimpleNamespace(
        storage_root=tmp_path,
        is_admin=lambda uid: uid == 1,
    )
    with (
        patch.object(api_mod, "settings", fake_settings),
        patch("app.services.downloader.MediaDownloader") as dl_cls,
    ):
        dl_cls.return_value.generate_recap_preview.side_effect = fake_preview
        asyncio.run(api_mod.recap_preview_composite(req_gold, user=user, db=None))
        asyncio.run(api_mod.recap_preview_composite(req_cine, user=user, db=None))

    assert len(calls) == 2
    assert calls[0]["burn_subtitles"] is True
    assert calls[0]["sub_style"] == "yellow"
    assert calls[0]["sub_font_size"] == 13
    assert calls[1]["burn_subtitles"] is True
    assert calls[1]["sub_style"] == "cinematic"
    assert calls[1]["sub_font_size"] == 22


# ── Item 2: Step-4 summary names actual voice + size (pt) + speed (%) ───────


def test_item2_step4_summary_voice_by_engine_and_size_and_speed_via_node():
    # Round 13 retired #recap-step4-facts / refreshRecapStep4Facts and simplified
    # refreshRecapStep4Choices to clean localized voice names per engine.
    fns = "\n".join(
        [
            _locked_defaults_js(),
            _extract_fn(APP_JS, "_cleanStep4VoiceName"),
            "function normalizeEdgeVoiceKey(v) { return String(v || 'thiha').trim().toLowerCase() || 'thiha'; }",
            _extract_fn(APP_JS, "refreshRecapStep4Choices"),
        ]
    )
    script = f"""
const fs = require('fs');
const vm = require('vm');
const i18nSrc = fs.readFileSync({json.dumps(str(REPO / "app/webapp/static/js/i18n.js"))}, 'utf8');
const els = {{}};
const mkEl = (id, init={{}}) => (els[id] = Object.assign({{
  id, textContent: '', innerText: '',
  classList: {{ _s:new Set(), contains(c){{return this._s.has(c)}}, toggle(c,f){{if(f===undefined)f=!this._s.has(c); f?this._s.add(c):this._s.delete(c);}}, add(c){{this._s.add(c)}}, remove(c){{this._s.delete(c)}} }},
}}, init));
['recap-step4-choices','recap-info-title','recap-video-info-box','recap-step4-choice-voice',
 'recap-step4-choice-ratio','recap-step4-choice-title'].forEach(id => mkEl(id));
els['recap-info-title'].textContent = 'Demo Movie';

function runForLang(lang) {{
  const personaOptText = lang === 'my' ? 'ကိုမင်း (Ko Min) 🔥' : 'Ko Min (Male)';
  els['recap-persona-select'] = {{ value: 'komin', selectedOptions: [{{ textContent: personaOptText }}] }};
  els['recap-edge-voice-select'] = {{ value: 'thiha' }};
  const ctx = {{
    document: {{
      getElementById: (id) => els[id] || null,
      querySelector: (sel) => {{
        if (sel === '#recap-preview-ratio-group .ratio-btn.active') return {{ getAttribute: () => '9:16', textContent: '9:16' }};
        return null;
      }},
    }},
    window: {{}},
    localStorage: {{ getItem: () => lang, setItem: () => {{}} }},
    state: {{
      recapTtsEngine: 'gemini',
      recapGeminiVoice: 'Algenib',
      recapEdgeVoice: 'thiha',
      recapVoice: 'komin',
      recapOwnVoiceSelected: false,
      recapRatio: '9:16',
    }},
    resolveGeminiPreviewVoice: () => ctx.state.recapGeminiVoice,
  }};
  vm.runInNewContext(i18nSrc + '; globalThis.__tr = translations;', ctx);
  ctx.t = (k) => (ctx.__tr[lang] && ctx.__tr[lang][k]) || (ctx.__tr.en && ctx.__tr.en[k]) || k;
  ctx.tf = (k, p={{}}) => {{ let s = ctx.t(k); for (const [a,b] of Object.entries(p)) s = s.replace('{{'+a+'}}', b); return s; }};
  vm.runInNewContext({json.dumps(fns)}, ctx);

  ctx.refreshRecapStep4Choices();
  const gemVoice = els['recap-step4-choice-voice'].textContent;

  ctx.state.recapTtsEngine = 'edge';
  ctx.refreshRecapStep4Choices();
  const edgeVoice = els['recap-step4-choice-voice'].textContent;

  ctx.state.recapTtsEngine = 'voxcpm';
  ctx.refreshRecapStep4Choices();
  const personaVoice = els['recap-step4-choice-voice'].textContent;

  ctx.state.recapOwnVoiceSelected = true;
  ctx.refreshRecapStep4Choices();
  const cloneVoice = els['recap-step4-choice-voice'].textContent;

  return {{ gemVoice, edgeVoice, personaVoice, cloneVoice, ratio: els['recap-step4-choice-ratio'].textContent }};
}}
console.log(JSON.stringify({{ my: runForLang('my'), en: runForLang('en') }}));
"""
    res = _run_node(script)
    assert 'id="recap-step4-choices"' in INDEX_HTML
    assert 'id="recap-fact-speed"' not in INDEX_HTML
    assert 'id="recap-fact-mirror"' not in INDEX_HTML
    assert 'id="recap-fact-canvas"' not in INDEX_HTML

    assert "ကိုအာကာ" in res["my"]["gemVoice"] and "ကိုမင်း" not in res["my"]["gemVoice"]
    assert "Ko Arkar" in res["en"]["gemVoice"] and "Ko Min" not in res["en"]["gemVoice"]
    assert "သီဟ" in res["my"]["edgeVoice"]
    assert "Thiha" in res["en"]["edgeVoice"]
    assert "ကိုမင်း" in res["my"]["personaVoice"]
    assert "Ko Min" in res["en"]["personaVoice"]
    assert "ကိုယ်ပိုင်အသံ" in res["my"]["cloneVoice"]
    assert "My Voice" in res["en"]["cloneVoice"]
    assert res["en"]["ratio"] == "9:16"


# ── Item 3: Admin job viewer download & stream links ────────────────────────


def test_item3_admin_job_viewer_links_wired_via_node():
    fns = "\n".join(
        [
            _extract_fn(APP_JS, "urlCarriesStreamToken"),
            _extract_fn(APP_JS, "escapeHtml"),
            _extract_fn(APP_JS, "brandedDlName"),
            _extract_fn(APP_JS, "signedAdminHref"),
            _extract_fn(APP_JS, "openAdminMediaModal"),
            _extract_fn(APP_JS, "renderAdminQueueData"),
            _extract_fn(APP_JS, "renderAdminHistoryData"),
        ]
    )
    script = f"""
const vm = require('vm');
const els = {{}};
const mkEl = (id) => (els[id] = {{
  id, innerHTML: '', innerText: '', className: '',
  classList: {{ _s:new Set(), contains(c){{return this._s.has(c)}}, add(c){{this._s.add(c)}}, remove(c){{this._s.delete(c)}} }},
  removeAttribute(){{}}, setAttribute(){{}},
  querySelectorAll: () => [],
  querySelector: () => null,
}});
['admin-media-modal','admin-media-badge','admin-media-title','admin-media-body','admin-media-deliverables',
 'admin-queue-list','admin-global-history-list'].forEach(mkEl);

const ctx = {{
  document: {{ getElementById: (id) => els[id] || null }},
  window: {{}},
  haptic: () => {{}},
  t: (k) => k,
  tf: (k) => k,
  jobStatusText: (s) => s,
  displayJobError: (e) => e || '',
  admStateEmpty: () => {{}},
  wireAdminMediaButtons: () => {{}},
  adminQueueStatusFilter: 'all',
  adminHistFilter: {{}},
  setAuthMediaSource: () => Promise.resolve(true),
  JSON, String, Object,
}};
vm.runInNewContext({json.dumps(fns)}, ctx);

const deliv = {{
  video: '/api/recap/download/rc_1794747627_9c69fc/video?t=tokV',
  audio: '/api/recap/download/rc_1794747627_9c69fc/audio?t=tokA',
  srt: '/api/recap/download/rc_1794747627_9c69fc/srt?t=tokS',
  script: '/api/recap/download/rc_1794747627_9c69fc/script?t=tokT',
}};
const streamUrl = '/api/recap/stream/rc_1794747627_9c69fc/video?t=tokV';
ctx.openAdminMediaModal('rc_1794747627_9c69fc', 'RECAP', streamUrl, JSON.stringify(deliv), 'Demo');
const modalHtml = els['admin-media-deliverables'].innerHTML;

ctx.renderAdminQueueData({{
  jobs: [{{ id: 'rc_1794747627_9c69fc', status: 'SUCCESS', engine: 'recap', job_type: 'RECAP', user_id: 42, username: 'admin', created_ago: '1m', download_url: streamUrl, deliverables: deliv }}],
  counts: {{}},
}});
const queueHtml = els['admin-queue-list'].innerHTML;

ctx.renderAdminHistoryData({{
  history: [{{ id: 'rc_1794747627_9c69fc', status: 'SUCCESS', engine: 'recap', job_type: 'RECAP', user_id: 42, username: 'admin', time_ago: '1m', download_url: streamUrl, deliverables: deliv }}],
}});
const histHtml = els['admin-global-history-list'].innerHTML;

const unsigned = ctx.signedAdminHref('/api/recap/download/rc_1/video');
console.log(JSON.stringify({{ modalHtml, queueHtml, histHtml, unsigned }}));
"""
    res = _run_node(script)
    assert 'href="#"' not in res["modalHtml"]
    assert 'href="/api/recap/download/rc_1794747627_9c69fc/video?t=tokV"' in res["modalHtml"]
    assert 'href="/api/recap/download/rc_1794747627_9c69fc/audio?t=tokA"' in res["modalHtml"]
    assert 'href="/api/recap/download/rc_1794747627_9c69fc/srt?t=tokS"' in res["modalHtml"]
    assert 'href="/api/recap/download/rc_1794747627_9c69fc/script?t=tokT"' in res["modalHtml"]
    assert 'href="/api/recap/stream/rc_1794747627_9c69fc/video?t=tokV"' in res["modalHtml"]
    assert 'href="/api/recap/stream/rc_1794747627_9c69fc/video?t=tokV"' in res["queueHtml"]
    assert 'href="/api/recap/stream/rc_1794747627_9c69fc/video?t=tokV"' in res["histHtml"]
    assert res["unsigned"] == "#"


@pytest.mark.asyncio
async def test_item3_admin_access_control_preserved(db_session):
    non_admin = User(id=999001, first_name="Regular")
    db_session.add(non_admin)
    await db_session.commit()
    fake_settings = SimpleNamespace(is_admin=lambda uid: uid == 123456)
    with patch.object(api_mod, "settings", fake_settings):
        with pytest.raises(HTTPException) as exc:
            await api_mod.require_admin(x_telegram_init_data="", user=non_admin)
        assert exc.value.status_code == 403


# ── Item 4: Mirror visibility at Step 2 and Step 4 ──────────────────────────


def test_item4_mirror_visibility_step2_and_step4_via_node():
    # Round 10 B6/B7 & Round 13: Step 2 summary bar, transform expander & Step 4 facts bar removed;
    # mirror stays locked true in Pro defaults and refreshRecapStep2Summary still reports ratio · blur.
    assert 'id="recap-step2-summary"' not in INDEX_HTML
    assert 'id="recap-preview-transform-group"' not in INDEX_HTML
    fns = "\n".join(
        [
            _locked_defaults_js(),
            _extract_fn(APP_JS, "refreshRecapStep2Summary"),
        ]
    )
    script = f"""
const fs = require('fs');
const vm = require('vm');
const i18nSrc = fs.readFileSync({json.dumps(str(REPO / "app/webapp/static/js/i18n.js"))}, 'utf8');
const els = {{}};
const mkClassList = () => ({{ _s:new Set(), contains(c){{return this._s.has(c)}}, toggle(c,f){{if(f===undefined)f=!this._s.has(c); f?this._s.add(c):this._s.delete(c);}}, add(c){{this._s.add(c)}}, remove(c){{this._s.delete(c)}} }});
const mkGroup = (id) => {{
  const valEl = {{ innerText: '', classList: mkClassList() }};
  els[id] = {{ id, querySelector: (s) => s === '.dd-acc-val' ? valEl : null, _val: valEl, classList: mkClassList() }};
}};
['recap-preview-ratio-group','recap-story-style-acc','recap-pace-acc'].forEach(mkGroup);

const ctx = {{
  document: {{ getElementById: (id) => els[id] || null, querySelector: () => null }},
  window: {{}},
  localStorage: {{ getItem: () => 'en', setItem: () => {{}} }},
  state: {{ recapRatio: '9:16', recapMirror: true, recapFontSize: 16, recapSpeed: '+12%' }},
}};
vm.runInNewContext(i18nSrc + '; globalThis.__tr = translations;', ctx);
ctx.t = (k) => (ctx.__tr.en && ctx.__tr.en[k]) || k;
vm.runInNewContext({json.dumps(fns)}, ctx);

ctx.refreshRecapStep2Summary();
const on = {{
  ratioAcc: els['recap-preview-ratio-group']._val.innerText || els['recap-preview-ratio-group']._val.textContent,
  paceAcc: els['recap-pace-acc']._val.innerText || els['recap-pace-acc']._val.textContent,
}};
console.log(JSON.stringify({{ on }}));
"""
    res = _run_node(script)
    assert res["on"]["ratioAcc"] == "9:16 · blur"
    assert res["on"]["paceAcc"] == "+12%"


# ── Item 6: Round 10 B4 removed fill picker & fill disabled note ────────────


def test_item6_disabled_fill_picker_shows_i18n_note_via_node():
    assert 'id="recap-fill-picker"' not in INDEX_HTML
    assert 'id="recap-fill-note"' not in INDEX_HTML
    assert "syncFillPickerState" not in APP_JS
    assert 'const RECAP_LOCKED_FILL = "blur"' in APP_JS


# ── Item 7: Subtitle size slider ticks & ±0.5 pt snapping ───────────────────


def test_item7_font_size_slider_ticks_and_snap_via_node():
    assert 'id="slider-font-size"' in INDEX_HTML
    assert 'step="0.1"' in INDEX_HTML
    assert 'id="slider-font-size-ticks"' in INDEX_HTML
    for stop in ("<span>13</span>", "<span>16</span>", "<span>19</span>", "<span>22</span>"):
        assert stop in INDEX_HTML
    assert ".slider-ticks" in STYLE_CSS

    fn = _extract_fn(APP_JS, "syncSubtitleFontSize")
    script = f"""
const vm = require('vm');
const slider = {{ value: '16' }};
const valEl = {{ innerText: '' }};
const ctx = {{
  document: {{ getElementById: (id) => id === 'slider-font-size' ? slider : (id === 'val-font-size' ? valEl : null) }},
  state: {{ recapFontSize: 16 }},
  updateSubtitleVisualPreview: () => {{}},
  scheduleRecapCompositeRefresh: () => {{}},
  refreshRecapStep2Summary: () => {{}},
  parseFloat, Math, Number,
}};
vm.runInNewContext({json.dumps(fn)}, ctx);
const samples = ['12.5','13.4','14.2','15.5','16.4','17.3','18.5','19.5','20.0','21.6','22.5'];
const results = samples.map(v => {{
  ctx.syncSubtitleFontSize(v, true);
  return [v, ctx.state.recapFontSize, valEl.innerText, String(slider.value)];
}});
console.log(JSON.stringify({{ results }}));
"""
    res = _run_node(script)
    expected = {
        "12.5": 13,
        "13.4": 13,
        "14.2": 14,
        "15.5": 16,
        "16.4": 16,
        "17.3": 17,
        "18.5": 19,
        "19.5": 19,
        "20.0": 20,
        "21.6": 22,
        "22.5": 22,
    }
    for raw, size, label, slider_val in res["results"]:
        assert size == expected[raw]
        assert label == f"{expected[raw]}pt"
        assert slider_val == str(expected[raw])


# ── Item 8: Upload single status at pct >= 100 ──────────────────────────────


def test_item8_upload_single_status_at_100_pct_via_node():
    fn = _extract_fn(APP_JS, "setRecapInfoUploadState")
    assert 'if (phase !== "processing") { phase = "processing"; setRecapInfoUploadState("processing"); }' in APP_JS

    script = f"""
const fs = require('fs');
const vm = require('vm');
const i18nSrc = fs.readFileSync({json.dumps(str(REPO / "app/webapp/static/js/i18n.js"))}, 'utf8');
const dur = {{ className: '', innerText: '' }};
const sub = {{ className: '', innerText: '' }};
const ctx = {{
  document: {{ getElementById: (id) => id === 'recap-info-dur' ? dur : (id === 'recap-info-sub' ? sub : null) }},
  window: {{}},
  localStorage: {{ getItem: () => 'my', setItem: () => {{}} }},
}};
vm.runInNewContext(i18nSrc + '; globalThis.__tr = translations;', ctx);
ctx.t = (k) => (ctx.__tr.my && ctx.__tr.my[k]) || k;
vm.runInNewContext({json.dumps(fn)}, ctx);

ctx.setRecapInfoUploadState('pending');
const pendingText = sub.innerText;
ctx.setRecapInfoUploadState('processing');
const processingText = sub.innerText;
ctx.setRecapInfoUploadState('ready');
const readyText = sub.innerText;
console.log(JSON.stringify({{ pendingText, processingText, readyText }}));
"""
    res = _run_node(script)
    assert "မရောက်သေးပါ" in res["pendingText"]
    assert "မရောက်သေးပါ" not in res["processingText"]
    assert "ပြင်ဆင်နေသည်" in res["processingText"]
    assert res["readyText"] != res["processingText"]


def test_new_i18n_keys_complete_in_my_and_en():
    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(REPO / "app/webapp/static/js/i18n.js"))}, 'utf8');
const ctx = {{
  document: {{}},
  window: {{}},
  localStorage: {{ getItem: () => 'my', setItem: () => {{}} }},
}};
vm.runInNewContext(src + '; globalThis.__tr = translations;', ctx);
console.log(JSON.stringify({{ my: ctx.__tr.my, en: ctx.__tr.en }}));
"""
    res = _run_node(script)
    for key in ("recap_fact_mirror_on", "recap_info_processing"):
        assert str(res["my"].get(key) or "").strip(), f"Missing my.{key}"
        assert str(res["en"].get(key) or "").strip(), f"Missing en.{key}"
