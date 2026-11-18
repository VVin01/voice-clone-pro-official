"""Polish Pass 1 (2026-11-14, r148) — Design, layout & tier-truthful copy checks."""
import json
import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]
INDEX_HTML = (REPO / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "app" / "webapp" / "static" / "css" / "style.css").read_text(encoding="utf-8")
APP_JS = (REPO / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "app" / "webapp" / "static" / "js" / "i18n.js").read_text(encoding="utf-8")


def _extract_fn(src: str, name: str) -> str:
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
    raise AssertionError(f"Unclosed function {name}")


def test_r148_cache_token_and_budgets():
    assert INDEX_HTML.count("?v=20261118_r164") == 4
    assert "r147" not in INDEX_HTML

    total_lf = sum(
        len((REPO / p).read_bytes().replace(b"\r\n", b"\n"))
        for p in (
            "app/webapp/static/css/style.css",
            "app/webapp/static/js/app.js",
            "app/webapp/static/js/i18n.js",
        )
    )
    assert total_lf <= 1_650_000, f"Asset LF budget exceeded: {total_lf}"
    assert STYLE_CSS.count("{") == STYLE_CSS.count("}")

    marker = "VCP UI REBUILD V2 — PHASE 3"
    tail = STYLE_CSS.split(marker, 1)[1]
    assert "backdrop-filter" not in tail


def test_i18n_parity_for_polish_pass_1_keys():
    proc = subprocess.run(
        [
            "node",
            "-e",
            """
const vm = require('vm');
const fs = require('fs');
const i18nSrc = fs.readFileSync(process.argv[1], 'utf8');
const ctx = { window: {}, document: {}, localStorage: { getItem: () => null, setItem: () => {} } };
vm.runInNewContext(i18nSrc + '; globalThis.__tr = translations;', ctx);
const tr = ctx.__tr;
const myKeys = Object.keys(tr.my).sort();
const enKeys = Object.keys(tr.en).sort();
console.log(JSON.stringify({ equal: JSON.stringify(myKeys) === JSON.stringify(enKeys), count: myKeys.length, my: tr.my, en: tr.en }));
""",
            str(REPO / "app" / "webapp" / "static" / "js" / "i18n.js"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    data = json.loads(proc.stdout.strip())
    assert data["equal"] is True
    for k in (
        "home_tile_tts_period_cap",
        "home_tile_voice_period_paid",
        "home_tile_recap_period_paid",
        "home_tile_period_unlimited",
        "tts_balance_line_admin",
        "tts_balance_line_paid",
        "tts_balance_line_free_daily",
        "admin_plan_name_MIN_10",
        "admin_plan_name_all_in_one_master",
        "adm_ov_tap_slips",
        "adm_ov_tap_users",
        "adm_ov_tap_jobs",
    ):
        assert k in data["my"] and k in data["en"]


def test_1a_home_tiles_tier_truthful_via_node():
    fn_code = "\n".join(
        [
            _extract_fn(APP_JS, "formatTtsTabBalanceLine"),
            _extract_fn(APP_JS, "paintHomeTilePeriods"),
            _extract_fn(APP_JS, "paintTtsTodaySurfaces"),
            _extract_fn(APP_JS, "paintTtsChipFromQuota"),
        ]
    )
    script = f"""
const fs = require('fs');
const vm = require('vm');
const i18nSrc = fs.readFileSync({json.dumps(str(REPO / 'app' / 'webapp' / 'static' / 'js' / 'i18n.js'))}, 'utf8');
function mkEl(init={{}}) {{ return Object.assign({{ textContent: '', innerText: '', _hidden: false, classList: {{ add(c){{ if(c==='hidden') this._h=true; }}, remove(c){{ if(c==='hidden') this._h=false; }}, toggle(c,v){{ if(c==='hidden') this._h=Boolean(v); }} }} }}, init); }}
function evalCase(lang, userData, quotaData) {{
  const els = {{
    '#home-free-tts-value': mkEl(),
    '#home-free-voice-value': mkEl(),
    '#home-free-recap-value': mkEl(),
    '#home-free-tts-note': mkEl(),
    '#tts-balance-line': mkEl(),
    '#tts-period': mkEl(),
    '#voice-period': mkEl(),
    '#recap-period': mkEl(),
  }};
  const mapId = {{
    'home-free-tts-value': els['#home-free-tts-value'],
    'home-free-voice-value': els['#home-free-voice-value'],
    'home-free-recap-value': els['#home-free-recap-value'],
    'home-free-tts-note': els['#home-free-tts-note'],
    'tts-balance-line': els['#tts-balance-line'],
  }};
  const mapQs = {{
    '#home-free-card [data-i18n="home_free_tts_period"]': els['#tts-period'],
    '#home-free-card [data-i18n="home_free_voice_period"]': els['#voice-period'],
    '#home-free-card [data-i18n="home_free_recap_period"]': els['#recap-period'],
  }};
  const ctx = {{
    window: {{}},
    state: {{ user: userData, lang }},
    document: {{
      getElementById: (id) => mapId[id] || null,
      querySelector: (sel) => mapQs[sel] || null,
    }},
    localStorage: {{ getItem: () => lang, setItem: () => {{}} }},
    paintHomeStatusStrip: () => {{}},
    paintHomeStatusStripFromQuota: () => {{}},
    t: null, tf: null,
  }};
  vm.runInNewContext(i18nSrc + '; globalThis.__tr = translations;', ctx);
  ctx.currentLang = lang;
  ctx.t = (k) => (ctx.__tr[lang] && ctx.__tr[lang][k]) || k;
  ctx.tf = (k, p={{}}) => {{ let s = ctx.t(k); for (const [a,b] of Object.entries(p)) s = s.replace('{{'+a+'}}', b); return s; }};
  vm.runInNewContext({json.dumps(fn_code)}, ctx);
  ctx.paintTtsTodaySurfaces(userData);
  if (quotaData) ctx.paintTtsChipFromQuota(quotaData);
  return {{
    ttsPeriod: els['#tts-period'].textContent,
    voicePeriod: els['#voice-period'].textContent,
    recapPeriod: els['#recap-period'].textContent,
    noteHidden: Boolean(els['#home-free-tts-note'].classList._h),
  }};
}}
const paid = evalCase('my', {{ is_vip: true, is_admin: false, tts_daily_limit_seconds: 3600, tts_today_remaining_seconds: 3540, tts_referral_seconds: 120, balance_minutes: 42.5, recap_credits: 12 }}, {{ is_admin: false, unlimited: false, daily_limit_minutes: 60, remaining_seconds: 3420, referral_seconds: 120 }});
const free = evalCase('my', {{ is_vip: false, is_admin: false, tts_daily_limit_seconds: 900, tts_today_remaining_seconds: 1020, tts_referral_seconds: 120, balance_minutes: 3.0, recap_credits: 3 }}, {{ is_admin: false, unlimited: false, daily_limit_minutes: 15, remaining_seconds: 900, referral_seconds: 120 }});
const admin = evalCase('my', {{ is_vip: true, is_admin: true, tts_daily_limit_seconds: null, tts_referral_seconds: 0 }}, {{ is_admin: true, unlimited: true, daily_limit_minutes: 0, remaining_seconds: null, referral_seconds: 0 }});
console.log(JSON.stringify({{ paid, free, admin }}));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
    res = json.loads(proc.stdout.strip())
    assert res["paid"]["ttsPeriod"] == "60 မိနစ် / နေ့စဉ်"
    assert res["paid"]["voicePeriod"] == "ဝယ်ယူပြီး လက်ကျန်"
    assert res["paid"]["recapPeriod"] == "ဝယ်ယူပြီး Credit"
    assert res["paid"]["noteHidden"] is True

    assert res["free"]["ttsPeriod"] == "15 မိနစ် / နေ့စဉ်"
    assert res["free"]["voicePeriod"] == "180 စက္ကန့် / လစဉ်"
    assert res["free"]["recapPeriod"] == "3 Credit / တစ်ကြိမ်"
    assert res["free"]["noteHidden"] is False

    assert res["admin"]["ttsPeriod"] == "အကန့်အသတ်မရှိ"
    assert res["admin"]["voicePeriod"] == "အကန့်အသတ်မရှိ"
    assert res["admin"]["recapPeriod"] == "အကန့်အသတ်မရှိ"
    assert res["admin"]["noteHidden"] is True


def test_1d_tts_balance_line_and_history_retention_via_node():
    fmt_code = _extract_fn(APP_JS, "formatTtsTabBalanceLine")
    ret_code = "\n".join(
        [
            _extract_fn(APP_JS, "computeHistoryExpiryState"),
            _extract_fn(APP_JS, "computeRetentionBadgeHtml"),
            _extract_fn(APP_JS, "hubStatusBadgeHtml"),
            _extract_fn(APP_JS, "hubStatusBorder"),
            _extract_fn(APP_JS, "hubHistoryDetailHtml") if "function hubHistoryDetailHtml" in APP_JS else "function hubHistoryDetailHtml(){return '';}",
            _extract_fn(APP_JS, "formatJobDuration") if "function formatJobDuration" in APP_JS else "function formatJobDuration(){return '1m';}",
            _extract_fn(APP_JS, "brandedDlName") if "function brandedDlName" in APP_JS else "function brandedDlName(){return 'a.mp4';}",
            _extract_fn(APP_JS, "hubHistoryRowHtml"),
        ]
    )
    combined_code = fmt_code + "\n" + ret_code
    script = f"""
const fs = require('fs');
const vm = require('vm');
const i18nSrc = fs.readFileSync({json.dumps(str(REPO / 'app' / 'webapp' / 'static' / 'js' / 'i18n.js'))}, 'utf8');
const ctx = {{ window: {{}}, document: {{}}, localStorage: {{ getItem: () => 'my', setItem: () => {{}} }}, escapeHtml: (s)=>String(s||''), hubOpenDetailId: null, state: {{ canUseAiThumbnail: false, aiThumbnailCapReached: false, user: null }} }};
vm.runInNewContext(i18nSrc + '; globalThis.__tr = translations;', ctx);
ctx.t = (k) => (ctx.__tr.my && ctx.__tr.my[k]) || k;
ctx.tf = (k, p={{}}) => {{ let s = ctx.t(k); for (const [a,b] of Object.entries(p)) s = s.replace('{{'+a+'}}', b); return s; }};
vm.runInNewContext({json.dumps(combined_code)}, ctx);
const adminLine = ctx.formatTtsTabBalanceLine({{ isAdmin: true, isVip: true, unlimited: true, refSec: 0, remainSec: 0 }});
const paidLine = ctx.formatTtsTabBalanceLine({{ isAdmin: false, isVip: true, unlimited: false, refSec: 120, remainSec: 3420 }});
const freeRefLine = ctx.formatTtsTabBalanceLine({{ isAdmin: false, isVip: false, unlimited: false, refSec: 120, remainSec: 900 }});
const rowFresh = ctx.hubHistoryRowHtml({{ id: 'rc_1', job_type: 'RECAP', status: 'SUCCESS', input_snippet: 'Test', voice_display: 'Thiha', created_at: new Date(Date.now() - 3*3600*1000).toISOString(), time_ago: '3h ago', deliverables: {{ video: '/dl' }} }});
console.log(JSON.stringify({{ adminLine, paidLine, freeRefLine, rowFresh }}));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
    res = json.loads(proc.stdout.strip())
    assert "Admin" in res["adminLine"]
    assert "မိတ်ဆက်ဆု" not in res["adminLine"]
    assert "VIP" in res["paidLine"]
    assert "မိတ်ဆက်ဆု" not in res["paidLine"]
    assert "မိတ်ဆက်ဆု" in res["freeRefLine"]
    assert "vcp-badge-info" in res["rowFresh"]
    assert "⏳" in res["rowFresh"]
    assert "နာရီ ကျန်" in res["rowFresh"]


def test_1b_pricing_cards_and_1c_overview_settings_dom():
    pricing_fn = _extract_fn(APP_JS, "renderAdminPricingList")
    assert "admin_plan_name_" in pricing_fn
    assert "adm-price-card" in pricing_fn
    assert "adm-price-controls" in pricing_fn

    # 1C Overview stat cards are tappable to their destinations
    for marker in (
        'onclick="showAdminSection(\'users\')"',
        'onclick="openAdminUsersPaid()"',
        'onclick="showAdminSection(\'payments\')"',
        'onclick="showAdminSection(\'jobs\')"',
    ):
        assert marker in INDEX_HTML

    # Overview nav tile keeps explicit label span inside #adm-nav
    assert 'data-adm-section="overview"' in INDEX_HTML
    assert 'data-i18n="adm_nav_overview"' in INDEX_HTML

    # Settings save bar is at the bottom of #adm-screen-settings (after #adm-tts-chain-card)
    idx_chain = INDEX_HTML.index('id="adm-tts-chain-card"')
    idx_save = INDEX_HTML.index('class="adm-settings-save-bar')
    assert idx_save > idx_chain
    assert 'id="btn-save-admin-settings"' in INDEX_HTML

    # 1D Recap Step 2 Preview = Output guard untouched
    fit_fn = _extract_fn(APP_JS, "ddFitPreviewFrame")
    assert "Math.min(300, parentW)" in fit_fn
    assert "window.innerHeight < 640" in fit_fn
