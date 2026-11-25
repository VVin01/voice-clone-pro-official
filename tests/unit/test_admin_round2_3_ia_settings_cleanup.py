"""Combined Admin Round 2 + Round 3 structural, lazy-load, IA, and cleanup tests."""
from __future__ import annotations

import ast
import inspect
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
ADMIN_JS = (ROOT / "app/webapp/static/js/admin.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")


def _tab_admin_html() -> str:
    return INDEX_HTML.split('<section id="tab-admin"', 1)[1].split("</section>", 1)[0]


def test_part_a_6tab_ia_lazy_admin_js_merged_jobs_and_user_detail():
    tab_admin = _tab_admin_html()

    # Exact 6-tab sub-nav with pure Burmese labels
    nav_html = tab_admin.split('id="adm-nav"', 1)[1].split("</div>", 1)[0]
    sections = re.findall(r'data-adm-section="([^"]+)"', nav_html)
    assert sections == ["overview", "payments", "jobs", "users", "broadcast", "settings"]
    for label in ("ခြုံငုံ", "ပြေစာ", "အလုပ်များ", "အသုံးပြုသူ", "ထုတ်လွှင့်မှု", "ဆက်တင်"):
        assert label in nav_html

    # admin_pro.js retired; admin.js loaded lazily ONLY for admins
    assert not (ROOT / "app/webapp/static/js/admin_pro.js").exists()
    assert (ROOT / "app/webapp/static/js/admin.js").exists()
    assert 'admin_pro.js' not in INDEX_HTML
    assert "/static/js/admin.js?v=" in APP_JS
    assert "20261125_admin_r23" in APP_JS
    assert "state.user?.is_admin" in APP_JS

    # Merged Jobs filter contract & retired redundant admin endpoints
    jobs_html = tab_admin.split('id="adm-screen-jobs"', 1)[1].split('id="adm-screen-users"', 1)[0]
    j_statuses = re.findall(r'data-jstatus="([^"]+)"', jobs_html)
    assert j_statuses == ["active", "SUCCESS", "FAILED", "all"]
    for label in ("လုပ်ဆောင်ဆဲ", "အောင်မြင်", "မအောင်မြင်", "အားလုံး"):
        assert label in jobs_html
    assert 'id="adm-purge-details"' in jobs_html

    from app.webapp import api as api_mod

    for dead_ep in ("get_admin_users_search", "get_admin_ops_queues", "get_admin_fleet_status"):
        assert not hasattr(api_mod, dead_ep), f"Retired endpoint {dead_ep} still present"

    # Consolidated User Detail: 1 Grants card, 1 Jobs list, 1 Unified Wallet Ledger
    detail_html = tab_admin.split('id="adm-user-detail"', 1)[1].split('id="adm-screen-broadcast"', 1)[0]
    assert detail_html.count('id="adm-detail-grants-card"') == 1
    assert detail_html.count('id="adm-user-jobs"') == 1
    assert detail_html.count('id="adm-detail-ledger"') == 1
    assert "daily_free_pending_seconds" in ADMIN_JS
    assert "referral_seconds" in ADMIN_JS
    assert "မပေးရသေး" in I18N_JS


def test_part_b1_settings_7_accordions_single_column_no_legacy_kpay_wave_or_parens():
    tab_admin = _tab_admin_html()
    settings_html = tab_admin.split('id="adm-screen-settings"', 1)[1]

    # Exact 7 top-level details.adm-accordion accordions
    top_accs = re.findall(
        r'<details\b[^>]*class="[^"]*adm-accordion[^"]*"[^>]*id="([^"]+)"',
        settings_html,
    )
    assert top_accs == [
        "adm-acc-pay-maint",
        "admp-banks-card",
        "adm-acc-pricing",
        "adm-acc-ai-bots",
        "adm-acc-quotas-gates",
        "adm-acc-tutorials",
        "adm-acc-diagnostics",
    ]

    # Zero multi-column layout classes inside #tab-admin
    for bad_cls in ("form-row-2", "admp-grid-2", "adm-price-specs", "gemini-chain-grid", "grid-cols-3"):
        assert bad_cls not in tab_admin, f"Multi-column class {bad_cls} still in #tab-admin"
    assert ".adm-price-specs" not in STYLE_CSS
    assert ".adm-price-specs" not in ADMIN_JS

    # Zero legacy static kpay/wave inputs in DOM and settings JS
    for legacy_id in ("adm-set-kpay-num", "adm-set-kpay-name", "adm-set-wave-num", "adm-set-wave-name"):
        assert legacy_id not in INDEX_HTML
        assert legacy_id not in ADMIN_JS
    save_fn = ADMIN_JS.split("async function saveAdminSettings()", 1)[1].split("window.saveAdminSettings =", 1)[0]
    for legacy_key in ("payment_kpay_number", "payment_kpay_name", "payment_wave_number", "payment_wave_name"):
        assert legacy_key not in save_fn

    # Zero parenthetical text / bilingual parenthetical headings inside #tab-admin
    for i, line in enumerate(tab_admin.splitlines(), 1):
        if "(" in line and ")" in line and not any(
            x in line for x in ("onclick=", "ontoggle=", "onchange=", "oninput=", "<!--")
        ):
            raise AssertionError(f"Parenthetical text in #tab-admin line {i}: {line.strip()}")


def test_part_b2_b4_diagnostics_drawer_lazy_single_fetch_and_overview_clean():
    tab_admin = _tab_admin_html()
    overview_html = tab_admin.split('id="adm-screen-overview"', 1)[1].split('id="adm-screen-payments"', 1)[0]
    assert 'id="admp-ops-health-card"' not in overview_html
    assert 'id="admin-stage-timings-card"' not in overview_html

    diag_html = tab_admin.split('id="adm-acc-diagnostics"', 1)[1].split('id="adm-settings-save-bar"', 1)[0]
    sub_diags = re.findall(
        r'<details\b[^>]*class="[^"]*adm-diag-sub[^"]*"[^>]*id="([^"]+)"',
        diag_html,
    )
    assert sub_diags == [
        "adm-settings-keys-details",
        "admin-stage-timings-card",
        "admp-ops-health-card",
        "adm-settings-activity-details",
        "adm-acc-models-runtime",
    ]
    # All 5 sub-panels start collapsed (no open attribute on <details class="adm-diag-sub"...>)
    for tag in re.findall(r'<details\b[^>]*class="[^"]*adm-diag-sub[^"]*"[^>]*>', diag_html):
        assert " open" not in tag

    # Eager Pricing & Stage Timings fetches removed from Settings/Overview entry; lazy on expand
    show_fn = ADMIN_JS.split("function showAdminSection(", 1)[1].split("window.showAdminSection =", 1)[0]
    load_data_fn = ADMIN_JS.split("async function loadAdminData(", 1)[1].split("function paintOverviewOpsFromStats(", 1)[0]
    load_set_fn = ADMIN_JS.split("async function loadAdminSettings(", 1)[1].split("function setSelectOrCustom(", 1)[0]
    assert "loadAdminStageTimings" not in load_data_fn
    assert "loadAdminStageTimings" not in show_fn
    assert "\n        loadAdminPricing();" not in show_fn
    assert 'if (document.getElementById("adm-acc-ai-bots")?.open)' in load_set_fn

    # Node harness proving single-fetch deduplication on Audit Log & Key Health expand + Group AI / Pricing functions present
    script = f"""
const fs = require("fs");
const vm = require("vm");
let fetchCalls = [];
const makeEl = (id, tag="div") => ({{
    id, tagName: tag.toUpperCase(), value: "", checked: false, open: false,
    innerText: "", textContent: "", innerHTML: "", disabled: false, style: {{}},
    dataset: {{}}, options: [],
    classList: {{ _s: new Set(), add(c){{this._s.add(c);}}, remove(c){{this._s.delete(c);}}, toggle(c,f){{if(f===undefined)f=!this._s.has(c);f?this._s.add(c):this._s.delete(c);}}, contains(c){{return this._s.has(c);}} }},
    addEventListener(){{}}, querySelectorAll(){{return [];}}, querySelector(){{return null;}},
    scrollIntoView(){{}}, appendChild(){{}}, remove(){{}}, closest(){{return null;}}
}});
const els = {{}};
for (const id of [
    "adm-acc-diagnostics","adm-settings-activity-details","adm-settings-keys-details",
    "admin-audit-list","admp-aud-prefix","admp-aud-q","adm-keys-summary","adm-keys-checked-at",
    "adm-keys-cached","adm-keys-accounts-btn","adm-keys-accounts","adm-nav","adm-settings-save-bar",
    "tab-admin","adm-screen-overview","adm-screen-payments","adm-screen-jobs","adm-screen-users",
    "adm-screen-broadcast","adm-screen-settings","adm-analytics-card","admp-banks-card",
    "adm-acc-pricing","adm-acc-ai-bots"
]) els[id] = makeEl(id);
const ctx = {{
    window: {{ _lastAdminOverview: {{ users: {{ real: 1 }} }}, addEventListener() {{}} }},
    document: {{
        getElementById: (id) => els[id] || null,
        querySelectorAll: () => [],
        addEventListener() {{}},
        createElement: (t) => makeEl(t, t),
    }},
    localStorage: {{ getItem: () => "my", setItem: () => {{}} }},
    state: {{ user: {{ is_admin: true }} }},
    t: (k) => k, tf: (k) => k, escapeHtml: (s) => String(s || ""),
    admFmtDate: () => "2026-11-25", admFmtYangon: () => "2026-11-25",
    getAuthHeaders: () => ({{}}), haptic: () => {{}}, showStudioToast: () => {{}},
    showStudioConfirm: (_m, cb) => cb && cb(), scheduleAdminQueuePoll: () => {{}},
    loadAdminJobs: () => {{}}, loadAdminUsersRecent: () => {{}}, loadAdminPendingSlips: () => {{}},
    setTimeout, clearTimeout, URLSearchParams, console,
    fetch: async (url) => {{ fetchCalls.push(url); return {{ ok: true, json: async () => ({{}}), text: async () => "[]" }}; }},
    fetchWithTimeout: async (url) => {{ fetchCalls.push(url); return {{ ok: true, json: async () => [] }}; }},
}};
vm.runInNewContext(fs.readFileSync({json.dumps(str(ROOT / "app/webapp/static/js/admin.js"))}, "utf8"), ctx);
// Simulate shortcut + repeated expand calls to loadDiagPanel('audit')
(async () => {{
    await ctx.loadDiagPanel("audit");
    await ctx.loadDiagPanel("audit");
    const auditCalls = fetchCalls.filter(u => u.includes("/api/admin/audit-log")).length;
    console.log(JSON.stringify({{
        auditCalls,
        hasGroupAi: typeof ctx.window.loadAdminGroupAiConsole === "function",
        hasSaveSheet: typeof ctx.window.saveGroupAiKnowledgeSheet === "function",
        hasResetSheet: typeof ctx.window.resetGroupAiKnowledgeSheet === "function",
        hasDiagOps: typeof ctx.window.loadDiagOpsHealth === "function",
    }}));
}})();
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    res = json.loads(proc.stdout.strip())
    assert res["auditCalls"] == 1
    assert res["hasGroupAi"] and res["hasSaveSheet"] and res["hasResetSheet"] and res["hasDiagOps"]

    # B4: GET /api/admin/group-ai switches to refresh_dynamic_settings_if_changed(db)
    from app.webapp import api as api_mod

    src = inspect.getsource(api_mod.get_admin_group_ai_console)
    assert "refresh_dynamic_settings_if_changed(db)" in src


def test_part_b3_dead_legacy_code_removed_and_live_callbacks_preserved():
    # custom_voice.py deleted and not imported/mounted
    assert not (ROOT / "app/handlers/custom_voice.py").exists()
    handlers_init = (ROOT / "app/handlers/__init__.py").read_text(encoding="utf-8")
    tree_init = ast.parse(handlers_init)
    imported_mods = [
        getattr(n, "module", "") or ""
        for n in ast.walk(tree_init)
        if isinstance(n, ast.ImportFrom)
    ] + [
        alias.name
        for n in ast.walk(tree_init)
        if isinstance(n, ast.Import)
        for alias in n.names
    ]
    assert not any("custom_voice" in m for m in imported_mods)
    assert "dp.include_router(custom_voice" not in handlers_init

    # Dead symbols absent from admin.py & keyboards.py
    admin_py = (ROOT / "app/handlers/admin.py").read_text(encoding="utf-8")
    tree_admin = ast.parse(admin_py)
    admin_names = {
        node.name
        for node in ast.walk(tree_admin)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    for dead_sym in (
        "AdminBroadcastFSM",
        "AdminTutorialFSM",
        "_fetch_pulse_metrics",
        "render_admin_pulse",
        "_render_user_welcome_fallthrough",
    ):
        assert dead_sym not in admin_names, f"Dead symbol {dead_sym} still in admin.py"
    assert "is_authorized_admin" in admin_names

    kb_py = (ROOT / "app/ui/keyboards.py").read_text(encoding="utf-8")
    tree_kb = ast.parse(kb_py)
    kb_names = {
        node.name
        for node in ast.walk(tree_kb)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    for dead_kb in (
        "admin_tutorial_mgmt_kb",
        "admin_tutorial_confirm_remove_kb",
        "admin_tutorial_cancel_kb",
        "custom_voice_hub_kb",
        "custom_voice_vip_promo_kb",
        "custom_voice_cancel_kb",
    ):
        assert dead_kb not in kb_names, f"Dead keyboard {dead_kb} still in keyboards.py"

    # Global Invariant #2: live DM callback prefixes remain registered
    app_py_corpus = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (ROOT / "app").rglob("*.py")
    )
    for prefix in (
        "admin:pay_approve:",
        "admin:pay_reject:",
        "admin:pay_edit:",
        "admin:pay_edit_promo20:",
        "gai_esc",
        "gmod_undo",
    ):
        assert prefix in app_py_corpus, f"Live callback prefix missing: {prefix}"


def test_part_b_bundle_budget_and_cache_token_lockstep():
    assert INDEX_HTML.count("?v=20261125_admin_r23") == 5
    lf_sum = sum(
        len((ROOT / f).read_bytes().replace(b"\r\n", b"\n"))
        for f in ("app/webapp/static/css/style.css", "app/webapp/static/js/app.js", "app/webapp/static/js/i18n.js")
    )
    assert lf_sum <= 1_650_000, f"Core LF bundle exceeded cap: {lf_sum}"
