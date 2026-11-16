"""Quality Pass 1 (2026-11-14, r149) — Copy, Prompts, TTS, Subtitles, Queue & Steps checks."""
import ast
import json
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

INDEX_HTML = (REPO / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "app" / "webapp" / "static" / "css" / "style.css").read_text(encoding="utf-8")
APP_JS = (REPO / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "app" / "webapp" / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
API_PY = (REPO / "app" / "webapp" / "api.py").read_text(encoding="utf-8")
PLANNER_PY = (REPO / "app" / "services" / "recap_engine" / "recap_planner.py").read_text(encoding="utf-8")
CORE_PY = (REPO / "app" / "services" / "recap_engine" / "core.py").read_text(encoding="utf-8")
PIPELINE_PY = (REPO / "app" / "services" / "recap_engine" / "recap_pipeline.py").read_text(encoding="utf-8")
PNG_SUBS_PY = (REPO / "app" / "services" / "recap_engine" / "png_subtitles.py").read_text(encoding="utf-8")


def _extract_js_fn(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    depth = 0
    in_body = False
    for idx in range(start, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
            in_body = True
        elif ch == "}":
            depth -= 1
            if in_body and depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"Unclosed JS function {name}")


def _extract_py_fn(src: str, name: str) -> str:
    tree = ast.parse(src)
    lines = src.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    raise AssertionError(f"Missing Python function {name}")


def test_2a_locale_and_i18n_parity_and_copy_hygiene():
    my_msg = json.loads((REPO / "locales" / "my" / "messages.json").read_text(encoding="utf-8"))
    en_msg = json.loads((REPO / "locales" / "en" / "messages.json").read_text(encoding="utf-8"))
    assert set(my_msg) == set(en_msg)
    for k, v in list(my_msg.items()) + list(en_msg.items()):
        assert "50MB" not in str(v), f"Use consistent '50 MB' spacing in {k}"

    proc = subprocess.run(
        [
            "node",
            "-e",
            """
const vm = require('vm');
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const ctx = { window: {}, document: {}, localStorage: { getItem: () => null, setItem: () => {} } };
vm.runInNewContext(src + '; globalThis.__tr = translations;', ctx);
const my = ctx.__tr.my, en = ctx.__tr.en;
const myK = Object.keys(my).sort(), enK = Object.keys(en).sort();
if (JSON.stringify(myK) !== JSON.stringify(enK)) process.exit(2);
for (const [k, v] of Object.entries(my).concat(Object.entries(en))) {
  if (k.startsWith('toast_') && /^[✅❌⚠️ℹ️🚧💾✂️💡]/.test(String(v))) {
    console.error('Bad toast prefix:', k, v);
    process.exit(3);
  }
}
console.log(JSON.stringify({ count: myK.length, hasWaiting: 'live_job_status_waiting_choice' in my }));
""",
            str(REPO / "app" / "webapp" / "static" / "js" / "i18n.js"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    info = json.loads(proc.stdout.strip())
    assert info["count"] >= 1370
    assert info["hasWaiting"] is True
    assert API_PY.count('detail="ဒီလင့်ခ်ကို အသုံးပြု၍ မရပါခင်ဗျာ"') == 5
    assert "အဆင့်မြှင့်တင်ပါ" not in API_PY


def test_2a_tts_tab_tier_gating_via_node():
    fn_code = "\n".join(
        [
            _extract_js_fn(APP_JS, "formatTtsTabBalanceLine"),
            _extract_js_fn(APP_JS, "paintHomeTilePeriods"),
            _extract_js_fn(APP_JS, "paintTtsTodaySurfaces"),
            _extract_js_fn(APP_JS, "paintTtsCostHintTiered"),
        ]
    )
    i18n_path = json.dumps(str(REPO / "app" / "webapp" / "static" / "js" / "i18n.js"))
    script = f"""
const fs = require('fs');
const vm = require('vm');
const i18nSrc = fs.readFileSync({i18n_path}, 'utf8');
function mkEl() {{
  const cl = {{
    _hidden: false,
    add(c) {{ if (c === 'hidden') this._hidden = true; }},
    remove(c) {{ if (c === 'hidden') this._hidden = false; }},
    toggle(c, v) {{ if (c === 'hidden') this._hidden = Boolean(v); }},
  }};
  return {{
    textContent: '', innerText: '',
    classList: cl,
  }};
}}
function evalTier(opts, userData) {{
  const els = {{
    note: mkEl(),
    ttsVal: mkEl(),
    voiceVal: mkEl(),
    recapVal: mkEl(),
    chip: mkEl(),
    ttsPeriod: mkEl(),
    voicePeriod: mkEl(),
    recapPeriod: mkEl(),
    shortHint: mkEl(),
    moreHint: mkEl(),
  }};
  const byId = {{
    'home-free-tts-note': els.note,
    'home-free-tts-value': els.ttsVal,
    'home-free-voice-value': els.voiceVal,
    'home-free-recap-value': els.recapVal,
    'user-tts-bal': els.chip,
  }};
  const byQs = {{
    '#home-free-card [data-i18n="home_free_tts_period"]': els.ttsPeriod,
    '#home-free-card [data-i18n="home_free_voice_period"]': els.voicePeriod,
    '#home-free-card [data-i18n="home_free_recap_period"]': els.recapPeriod,
    '#tab-tts [data-i18n="tts_cost_hint"]': els.shortHint,
    '#tts-cost-hint-more [data-i18n="tts_cost_hint_more"]': els.moreHint,
  }};
  const ctx = {{
    window: {{ _lastTtsQuota: null }},
    state: {{ user: userData }},
    localStorage: {{ getItem: () => 'my', setItem: () => {{}} }},
    paintHomeStatusStrip: () => {{}},
    document: {{
      getElementById: (id) => byId[id] || null,
      querySelector: (s) => byQs[s] || null,
    }},
  }};
  vm.runInNewContext(i18nSrc + '\\n' + {json.dumps(fn_code)}, ctx);
  ctx.paintTtsTodaySurfaces(userData);
  return {{
    line: ctx.formatTtsTabBalanceLine(opts),
    noteHidden: els.note.classList._hidden,
    ttsPeriod: els.ttsPeriod.textContent,
    ttsVal: els.ttsVal.innerText,
    shortHint: els.shortHint.textContent,
    moreHint: els.moreHint.textContent,
  }};
}}
const admin = evalTier(
  {{ isAdmin: true, isVip: false, unlimited: true, refSec: 120, remainSec: 0 }},
  {{ is_admin: true, is_vip: false, tts_daily_limit_seconds: 0, tts_balance_seconds: 120, balance_minutes: 999, recap_credits: 99 }}
);
const vip = evalTier(
  {{ isAdmin: false, isVip: true, unlimited: false, refSec: 0, remainSec: 3600 }},
  {{ is_admin: false, is_vip: true, tts_daily_limit_seconds: 3600, tts_today_remaining_seconds: 3600, tts_referral_seconds: 0, tts_balance_seconds: 0, balance_minutes: 60, recap_credits: 5 }}
);
const freeGift = evalTier(
  {{ isAdmin: false, isVip: false, unlimited: false, refSec: 120, remainSec: 900 }},
  {{ is_admin: false, is_vip: false, tts_daily_limit_seconds: 900, tts_today_remaining_seconds: 900, tts_referral_seconds: 120, balance_minutes: 3, recap_credits: 3 }}
);
console.log(JSON.stringify({{ admin, vip, freeGift }}));
"""
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    res = json.loads(proc.stdout.strip())
    assert res["admin"]["noteHidden"] is True
    assert "မိတ်ဆက်ဆု" not in res["admin"]["line"]
    assert "မိတ်ဆက်ဆု" not in res["admin"]["shortHint"]
    assert "မိတ်ဆက်ဆု" not in res["admin"]["moreHint"]
    assert "Admin" in res["admin"]["shortHint"]
    assert res["admin"]["ttsVal"] == "∞"
    assert res["vip"]["noteHidden"] is True
    assert "မိတ်ဆက်ဆု" not in res["vip"]["line"]
    assert "မိတ်ဆက်ဆု" not in res["vip"]["shortHint"]
    assert res["freeGift"]["noteHidden"] is False
    assert "မိတ်ဆက်ဆု" in res["freeGift"]["line"]
    assert "မိတ်ဆက်ဆု" in res["freeGift"]["shortHint"]


def test_2b_recap_styles_hooks_audit_flags_and_fonts():
    assert PLANNER_PY.count("DISTINCT IDENTITY:") == 5
    assert "MANDATORY 3-SECOND HOOK (IDs 1–2 FOR EVERY STYLE)" in PLANNER_PY
    for key in ('"viral":', '"funny":', '"pov":', '"dramatic":', '"narrator":'):
        assert key in PLANNER_PY
    assert "SEAMLESS CONTINUATION" in PLANNER_PY
    assert "~10 to 14 Burmese syllables per second" in PLANNER_PY

    from app.services.recap_engine import recap_planner

    clean = [
        {"id": 1, "source_text": "He ran outside.", "translation": "သူ အပြင်ကို အမြန် ပြေးထွက်သွားတယ်။"},
        {"id": 2, "source_text": "She called him.", "translation": "သူမက နောက်ကနေ လှမ်းခေါ်လိုက်တယ်။"},
    ]
    assert recap_planner._batch_audit_flags(clean, [], []) == []

    flags_bookish = recap_planner._batch_audit_flags(
        [{"id": 1, "source_text": "He left.", "translation": "သူ ထွက်သွားသည်"}], [], []
    )
    assert any("bookish" in f for f in flags_bookish)

    flags_tagyi = recap_planner._batch_audit_flags(
        [{"id": 1, "source_text": "She cried loudly.", "translation": "သူမ ငိုကြွေးတကြီး အော်ဟစ်လိုက်တယ်"}], [], []
    )
    assert any("-တကြီး" in f for f in flags_tagyi)

    flags_aed = recap_planner._batch_audit_flags(
        [{"id": 1, "source_text": "That room.", "translation": "အဲ့ဒီ အခန်းထဲကို ဝင်သွားတယ်"}], [], []
    )
    assert any("အဲ့ဒီ" in f for f in flags_aed)

    flags_digits = recap_planner._batch_audit_flags(
        [{"id": 1, "source_text": "3 days later.", "translation": "3 ရက်အကြာမှာ ပြန်ရောက်လာတယ်"}], [], []
    )
    assert any("ASCII digits" in f for f in flags_digits)

    flags_khae = recap_planner._batch_audit_flags(
        [
            {"id": 1, "source_text": "One", "translation": "တစ်ခု ဖြစ်ခဲ့ပါတယ်"},
            {"id": 2, "source_text": "Two", "translation": "နှစ်ခု တွေ့ခဲ့ပါတယ်"},
            {"id": 3, "source_text": "Three", "translation": "သုံးခု သိခဲ့ပါတယ်"},
        ],
        [],
        [],
    )
    assert any("ခဲ့ပါတယ်" in f for f in flags_khae)

    fonts_dir = REPO / "app" / "assets" / "fonts"
    assert not (fonts_dir / "NotoSansMyanmar.ttf").exists()
    assert (fonts_dir / "NotoSansMyanmar-Regular.ttf").is_file()
    assert (fonts_dir / "NotoSansMyanmar-Bold.ttf").is_file()


def test_2c_tts_hardening_rotation_and_free_quota():
    from app.services import gemini_key_service, key_rotation, tts_service
    from app.services.recap_engine import gemini_tts_engine as gtts

    assert tts_service.TTS_CHUNK_CHARS == 400

    kh = key_rotation.KeyHealth()
    kh.mark_failure("key-alpha", model="gemini-3.8-flash-tts")
    assert kh.in_cooldown("key-alpha", model="gemini-3.8-flash-tts") is True
    assert kh.in_cooldown("key-alpha", model="gemini-3.1-flash-tts-preview") is False
    snapshot = kh.snapshot_state()
    assert any(k.startswith("gemini-3.8-flash-tts:") for k in snapshot["cooldowns"])

    kh.record_tts_accept("gemini-3.8-flash-tts")
    kh.record_tts_accept("gemini-3.8-flash-tts")
    kh.record_tts_dq_reject("gemini-3.8-flash-tts")
    stats = kh.tts_model_stats("gemini-3.8-flash-tts")
    assert stats == {"accept": 2, "dq_reject": 1, "total": 3, "dq_reject_rate": 0.3333}

    # Below 70% primary quota consumption, lite models trail primaries.
    keys = ["k1", "k2"]
    rotated_low = gtts._rotated_tts_models(health=kh, keys=keys)
    first_lite_idx = next(i for i, m in enumerate(rotated_low) if "lite" in m.lower())
    assert first_lite_idx == len(rotated_low) - 1

    # Exceed >= 70% primary quota usage -> lite model joins primary rotation.
    for k in keys:
        for _ in range(8):
            kh.record_usage(k, "gemini-3.8-flash-tts")
            kh.record_usage(k, "gemini-3.1-flash-tts-preview")
            kh.record_usage(k, "gemini-2.5-flash-preview-tts")
    primaries = tuple(m for m in gtts.TTS_MODELS if "lite" not in m.lower())
    assert gtts._primary_quota_fraction_used(primaries, kh, keys) >= 0.70

    # FREE-only quota gate: PURCHASED and admins always bypass FREE taste/budget locks.
    vip_ctx = gemini_key_service.GeminiRouteContext(
        user_id=1, tier="PURCHASED", is_admin=False,
        taste_used_seconds=99999.0, taste_limit_seconds=60.0,
        budget_used_requests=999, budget_limit_requests=10,
    )
    assert vip_ctx.pool_allowed is True
    assert vip_ctx.taste_blocked is False
    assert vip_ctx.taste_remaining_seconds is None

    free_blocked = gemini_key_service.GeminiRouteContext(
        user_id=2, tier="FREE", is_admin=False,
        taste_used_seconds=120.0, taste_limit_seconds=60.0,
        budget_used_requests=1, budget_limit_requests=10,
    )
    assert free_blocked.pool_allowed is False
    assert free_blocked.taste_blocked is True


def test_2d_and_2e_png_and_stage_telemetry_and_2h_capsule():
    assert 'img.save(str(_tmp), "PNG", compress_level=1)' in PNG_SUBS_PY
    assert 'img.save(str(out), "PNG", compress_level=1)' in PNG_SUBS_PY
    assert 'ThreadPoolExecutor(max_workers=2, thread_name_prefix="recap-png")' in CORE_PY

    for stage_key in ('"png":', '"overlay":', '"mux":'):
        assert stage_key in API_PY
    assert "Rendering subtitle PNG" in CORE_PY
    assert "Compositing video overlay" in CORE_PY
    assert 'timings_out["png"]' in CORE_PY
    assert 'timings_out["overlay"]' in CORE_PY
    assert '_stage_timings["mux"]' in CORE_PY
    for step_key in ("transcribe", "context", "translate", "plan"):
        assert f'stage_timings["{step_key}"]' in PLANNER_PY
    assert "stage_timings" in PIPELINE_PY

    # §2H full-width Recap steps capsule + r149 cache token
    assert ".recap-steps-capsule" in STYLE_CSS
    assert "flex: 1 1 0;" in STYLE_CSS
    assert "flex: 2.8 1 auto;" in STYLE_CSS
    assert INDEX_HTML.count("?v=20261117_r159") == 4
    assert "r148" not in INDEX_HTML


def test_2f_waiting_choice_active_guard_and_honest_progress():
    active_block_start = API_PY.index("_LIVE_ACTIVE_STATUSES = (")
    active_block = API_PY[active_block_start : active_block_start + 180]
    assert "WAITING_CHOICE" in active_block

    # Critical billing invariant: _recap_active_job_count must NOT count WAITING_CHOICE.
    active_cnt_src = _extract_py_fn(API_PY, "_recap_active_job_count")
    assert "WAITING_CHOICE" not in active_cnt_src

    cancel_recap_src = _extract_py_fn(API_PY, "cancel_recap_job")
    assert "WAITING_CHOICE" in cancel_recap_src

    cancel_queued_src = _extract_py_fn(API_PY, "cancel_queued_job")
    assert "JobStatus.WAITING_CHOICE.value" in cancel_queued_src
    assert "Only QUEUED, RUNNING, or WAITING_CHOICE jobs can be cancelled" in cancel_queued_src

    js_code = "\n".join(
        [
            _extract_js_fn(APP_JS, "paintProgressBarFill"),
            _extract_js_fn(APP_JS, "setSubmitButtonState"),
        ]
    )
    node_script = f"""
const vm = require('vm');
function mkEl(init={{}}) {{
  const classes = new Set();
  return Object.assign({{
    style: {{ width: '' }},
    innerText: '',
    disabled: false,
    dataset: {{}},
    attrs: {{}},
    _span: null,
    classList: {{
      toggle(c, on) {{ if (on) classes.add(c); else classes.delete(c); }},
      contains(c) {{ return classes.has(c); }},
    }},
    getAttribute(k) {{ return this.attrs[k] || null; }},
    querySelector(sel) {{ return sel === '.btn-text' ? this._span : null; }},
  }}, init);
}}
const barWait = mkEl(), barQ = mkEl(), barRunZero = mkEl(), barRunMid = mkEl(), barDone = mkEl();
const startBtn = mkEl({{ _span: mkEl({{ innerText: 'Recap စတင်မည်' }}) }});
let activeFlag = true;
const ctx = {{
  voiceSubmitGuard: {{ inFlight: false, submittedFingerprint: null }},
  recapSubmitGuard: {{ inFlight: false, submittedFingerprint: null }},
  liveJobsIsTerminal: (s) => ['SUCCESS','FAILED','CANCELLED','ERROR'].includes(String(s || '').toUpperCase()),
  recapJobActive: () => activeFlag,
  recapJobIsActive: () => activeFlag,
  refreshStickyCtas: () => {{}},
  t: (k) => k === 'submit_already_creating' ? 'ဖန်တီးနေပါတယ်' : k,
  document: {{ getElementById: (id) => id === 'btn-start-recap' ? startBtn : null }},
}};
vm.runInNewContext({json.dumps(js_code)}, ctx);
ctx.paintProgressBarFill(barWait, {{ status: 'WAITING_CHOICE', progress_percent: 65 }});
ctx.paintProgressBarFill(barQ, {{ status: 'QUEUED', progress_percent: 0 }});
ctx.paintProgressBarFill(barRunZero, {{ status: 'RUNNING', progress_percent: 0 }});
ctx.paintProgressBarFill(barRunMid, {{ status: 'RUNNING', progress_percent: 62 }});
ctx.paintProgressBarFill(barDone, {{ status: 'SUCCESS', progress_percent: 100 }});
ctx.setSubmitButtonState('recap');
const blocked = {{ disabled: startBtn.disabled, text: startBtn._span.innerText }};
activeFlag = false;
ctx.setSubmitButtonState('recap');
const freed = {{ disabled: startBtn.disabled, text: startBtn._span.innerText }};
console.log(JSON.stringify({{
  waitInd: barWait.classList.contains('indeterminate'),
  waitClass: barWait.classList.contains('is-waiting'),
  waitW: barWait.style.width,
  qInd: barQ.classList.contains('indeterminate'),
  run0Ind: barRunZero.classList.contains('indeterminate'),
  runMidInd: barRunMid.classList.contains('indeterminate'),
  runMidW: barRunMid.style.width,
  doneW: barDone.style.width,
  blocked,
  freed,
}}));
"""
    proc = subprocess.run(
        ["node", "-e", node_script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    out = json.loads(proc.stdout.strip())
    assert out["waitInd"] is True and out["waitClass"] is True and out["waitW"] == "100%"
    assert out["qInd"] is True
    assert out["run0Ind"] is True
    assert out["runMidInd"] is False and out["runMidW"] == "62%"
    assert out["doneW"] == "100%"
    assert out["blocked"]["disabled"] is True
    assert out["freed"]["disabled"] is False and out["freed"]["text"] == "Recap စတင်မည်"


def test_2f_and_2e_api_runtime_when_fastapi_installed():
    pytest.importorskip("fastapi")
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app.webapp import api as api_mod

    stage, sub, pct, step = api_mod._recap_stage_progress("Rendering subtitle PNGs 2/4", 84)
    assert stage == "png" and sub == "2/4" and 85 <= pct <= 89 and step == "3/4"
    stage_ov, _, pct_ov, step_ov = api_mod._recap_stage_progress("Compositing video overlay", pct)
    assert stage_ov == "overlay" and pct_ov >= pct and step_ov == "4/4"
    stage_mx, _, pct_mx, _ = api_mod._recap_stage_progress("Final mux", pct_ov)
    assert stage_mx == "mux" and pct_mx >= pct_ov

    now = datetime.now(timezone.utc)
    q_voice = SimpleNamespace(
        id="v_q", job_type="TTS", engine="edge_tts", status="QUEUED",
        preset_voice="Nilar", input_text="Sample voice text",
        created_at=now, finished_at=None, result_path=None,
    )
    w_recap = SimpleNamespace(
        id="rc_w", job_type="RECAP", engine="recap", status="WAITING_CHOICE",
        preset_voice="nilar", input_text="Sample recap video",
        created_at=now, finished_at=None, result_path=None,
    )
    p_voice = api_mod._live_job_payload(q_voice, now=now)
    p_recap = api_mod._live_job_payload(w_recap, now=now)
    assert p_voice["progress_percent"] == 0
    assert p_recap["status"] == "WAITING_CHOICE"
    assert p_recap["progress_percent"] == 0


def test_2g_subtitle_cue_timing_no_drift():
    from app.services.recap_engine import core

    # 5 phrases across 4.8s -> must never trigger 1.1s/1.8s artificial stretching
    # that overflows 4.8s or crushes the final cue to a sliver.
    seg_text = (
        "ပထမအခန်းထဲကို ဝင်လာချိန်မှာ "
        "အခန်းတစ်ခုလုံး မှောင်မည်းနေပြီး "
        "စားပွဲပေါ်က စာချုပ်တစ်ခုကို တွေ့လိုက်ရတယ် "
        "အဲဒီနောက် ရုတ်တရက် တံခါးပိတ်သံ ကြားလိုက်ရပြီး "
        "နောက်ကနေ လူတစ်ယောက် ချဉ်းကပ်လာခဲ့တယ်"
    )
    cues = core.split_burmese_subtitle_cues(
        0.0, 4.8, seg_text, max_cue_chars=28, lines_per_cue=1
    )
    assert len(cues) >= 5
    assert cues[0][0] == 0.0
    assert cues[-1][1] == 4.8
    total_chars = sum(max(1, len(c[2])) for c in cues)
    for idx, (s, e, cue_txt) in enumerate(cues):
        assert 0.0 <= s < e <= 4.8, (idx, s, e, cue_txt)
        expected_dur = 4.8 * (max(1, len(cue_txt)) / total_chars)
        assert abs((e - s) - expected_dur) <= 0.015, (idx, s, e, expected_dur)
        if idx > 0:
            assert s == cues[idx - 1][1], "Cues must remain contiguous with zero gap/overlap"

    # WordBoundary events coherently covering a 2-unit utterance shift the boundary accordingly.
    two_unit_text = "ပထမစာကြောင်း အတိုချုပ်၊ ဒုတိယစာကြောင်း အတိုချုပ်"
    char_only = core.split_burmese_subtitle_cues(
        0.0, 4.0, two_unit_text, max_cue_chars=25, lines_per_cue=1
    )
    assert len(char_only) == 2
    u1 = char_only[0][2].replace("\n", "")
    u2 = char_only[1][2].replace("\n", "")
    # Simulate slow first phrase (0.0 -> 3.0s) and brisk second phrase (3.0 -> 4.0s).
    wb_events = [
        {"start": 0.0, "end": 3.0, "text": u1},
        {"start": 3.0, "end": 4.0, "text": u2},
    ]
    wb_cues = core.split_burmese_subtitle_cues(
        0.0, 4.0, two_unit_text, max_cue_chars=25, lines_per_cue=1, word_timings=wb_events
    )
    assert len(wb_cues) == 2
    assert wb_cues[0][1] > char_only[0][1]
    assert wb_cues[0][1] >= 2.8 and wb_cues[1][1] == 4.0


def test_2e_progress_callback_arity_no_typeerror():
    from app.services.recap_engine import core

    three_arg_events = []
    one_arg_events = []
    var_arg_events = []

    def three_arg_cb(cur, tot, label):
        three_arg_events.append((cur, tot, label))

    def one_arg_cb(label):
        one_arg_events.append(label)

    def var_arg_cb(*args):
        var_arg_events.append(args)

    core._emit_progress_stage(three_arg_cb, "Rendering subtitle PNGs (12 cues)")
    core._emit_progress_stage(three_arg_cb, "Compositing video overlay")
    core._emit_progress_stage(one_arg_cb, "Compositing video overlay")
    core._emit_progress_stage(var_arg_cb, "Compositing video overlay")
    core._emit_progress_stage(None, "ignored")

    assert three_arg_events == [
        (1, 1, "Rendering subtitle PNGs (12 cues)"),
        (1, 1, "Compositing video overlay"),
    ]
    assert one_arg_events == ["Compositing video overlay"]
    assert var_arg_events == [("Compositing video overlay",)]

    # Verify every self.progress(...) call inside EdgeSmartSync passes 3 arguments.
    tree = ast.parse(CORE_PY)
    progress_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "progress"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    ]
    assert len(progress_calls) >= 7
    for call in progress_calls:
        assert len(call.args) == 3, f"self.progress at line {call.lineno} must pass 3 args"

    assert 'lambda _msg: self.progress(1, 1, _msg)' in CORE_PY
    assert 'self.progress(1, 1, "Muxing final video")' in CORE_PY

