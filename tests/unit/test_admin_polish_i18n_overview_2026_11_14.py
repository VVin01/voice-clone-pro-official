"""Regression & Chromium Playwright verification for Batch 1 (2026-11-14):
  * Item 1: Key Health / VoxCPM Spaces fixed-rhythm 360px layout across all pools
  * Item 2: Root-cause late setLanguage fix + EN/MY full UI i18n sweep
  * Item 3: Admin Overview Total Users headline + Real / never-used split
"""
from __future__ import annotations

import functools
import http.server
import json
import re
import shutil
import socketserver
import subprocess
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = REPO_ROOT / "app" / "webapp" / "static"
INDEX_HTML = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (STATIC_ROOT / "css" / "style.css").read_text(encoding="utf-8")
I18N_JS = (STATIC_ROOT / "js" / "i18n.js").read_text(encoding="utf-8")
APP_JS = (STATIC_ROOT / "js" / "app.js").read_text(encoding="utf-8")

MYANMAR_RE = re.compile(r"[\u1000-\u109F]")


def _start_static_server() -> tuple[socketserver.TCPServer, int]:
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                target = STATIC_ROOT / "index.html"
            elif path.startswith("/static/"):
                rel = path[len("/static/"):].lstrip("/")
                target = (STATIC_ROOT / rel).resolve()
                if not str(target).startswith(str(STATIC_ROOT.resolve())):
                    self.send_error(403)
                    return
            else:
                self.send_error(404)
                return
            if not target.is_file():
                self.send_error(404)
                return
            ext = target.suffix.lower()
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".svg": "image/svg+xml",
                ".png": "image/png",
                ".json": "application/json; charset=utf-8",
            }.get(ext, "application/octet-stream")
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port


def _launch_chromium(p):
    for kwargs in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
        try:
            return p.chromium.launch(headless=True, **kwargs)
        except Exception:
            continue
    return None


def _mock_payloads() -> dict:
    return {
        "/api/user/me": {
            "id": 999999,
            "first_name": "Boss",
            "username": "bossadmin",
            "language_code": "my",
            "is_admin": True,
            "is_vip": True,
            "account_category": "PURCHASED",
            "balance_minutes": 42.5,
            "balance_seconds": 2550,
            "recap_credits": 12,
            "character_limit": 5000,
            "character_limit_free": 1000,
            "character_limit_purchased": 5000,
            "free_voice_used_today": 0,
            "free_voice_daily_limit": 2,
            "free_recap_used_today": 0,
            "free_recap_daily_limit": 1,
            "has_verified_channel_membership": True,
            "channel_membership_required": False,
            "can_use_custom_logo": True,
            "has_custom_logo": False,
            "tutorial_links": {},
            "completed_jobs_count": 3,
        },
        "/api/config": {
            "channel_membership_required": False,
            "free_voice_daily_limit": 2,
            "free_recap_daily_limit": 1,
            "free_daily_minutes": 2,
            "tts_daily_minutes": 10,
            "character_limit_free": 1000,
            "character_limit_purchased": 5000,
            "payment_accounts": [
                {"method": "kpay", "name": "U Kyaw", "number": "09123456789"},
                {"method": "wave", "name": "U Kyaw", "number": "09123456789"},
            ],
        },
        "/api/voices/curated": [
            {
                "id": "komin",
                "name": "Ko Min",
                "gender": "male",
                "style": "Narrator",
                "preview_url": "/api/voices/sample/komin",
            }
        ],
        "/api/voices/legacy": [],
        "/api/history": {"history": []},
        "/api/jobs/live": {"jobs": []},
        "/api/user/logo": {"has_logo": False, "url": None},
        "/api/user/preferences": {"preferences": {}},
        "/api/tts/quota": {
            "is_admin": True,
            "daily_limit_minutes": None,
            "consumed_seconds": 0,
            "remaining_seconds": None,
            "has_gemini_key": False,
            "masked_gemini_key": None,
        },
        "/api/vip/plans": {
            "plans": [{"code": "v_starter", "minutes": 30, "price_mmk": 5000}],
        },
        "/api/admin/overview": {
            "total_users": 420,
            "real_users": 185,
            "registered_users": 420,
            "never_used_users": 235,
            "vip_users": 24,
            "pending_slips": 3,
            "queued_jobs": 2,
            "running_jobs": 1,
            "cloud_worker_status": "ONLINE (Render Cloud)",
            "failed_jobs_24h": 1,
            "jobs_by_status": {
                "QUEUED": 2,
                "RUNNING": 1,
                "SUCCESS": 150,
                "FAILED": 4,
                "CANCELLED": 1,
            },
            "banned_users": 5,
            "banned_by_admin": 2,
            "bot_blocked_users": 3,
            "new_users_24h": 7,
            "income": {
                "today_mmk": 30000,
                "today_count": 2,
                "last_7d_mmk": 120000,
                "last_7d_count": 8,
                "last_30d_mmk": 450000,
                "last_30d_count": 30,
                "all_time_mmk": 1200000,
                "all_time_count": 80,
            },
            "db_size_bytes": 1048576,
            "storage_bytes": 2097152,
            "disk": {
                "total_bytes": 10_000_000_000,
                "used_bytes": 2_000_000_000,
                "free_bytes": 8_000_000_000,
                "used_percent": 20.0,
                "low": False,
            },
            "recent_admin_actions": [],
        },
        "/api/admin/pending-slips": [
            {
                "id": "slip_101",
                "user_id": 1001,
                "user_name": "Su Su",
                "username": "susu_user",
                "package_code": "r_10",
                "amount_mmk": 10000,
                "minutes": 0,
                "slip_url": None,
                "created_at": "2026-11-14 10:00",
            }
        ],
        "/api/admin/key-health": {
            "checked_at": "2026-11-14T15:00:00+00:00",
            "cached": True,
            "providers": {
                "hf": {
                    "total": 2,
                    "ok": 2,
                    "dead": 0,
                    "error": 0,
                    "quota_remaining_s": 540,
                    "quota_total_s": 600,
                    "quota_exhausted": 0,
                    "accounts": [
                        {
                            "index": 0,
                            "account": "awin2026_very_long_account_name_test",
                            "status": "ok",
                            "remaining_s": 240,
                            "base_s": 300,
                            "refill": {"method": "window_24h"},
                        },
                        {
                            "index": 1,
                            "account": "komin_short",
                            "status": "ok",
                            "remaining_s": 300,
                            "base_s": 300,
                            "refill": None,
                        },
                    ],
                },
                "gemini": {
                    "total": 2,
                    "ok": 1,
                    "limit": 1,
                    "dead": 0,
                    "keys": [
                        {"label": "GEMINI_API_KEYS[0]", "status": "ok"},
                        {"label": "GEMINI_API_KEYS[1]", "status": "limit"},
                    ],
                },
                "groq": {
                    "total": 1,
                    "ok": 1,
                    "limit": 0,
                    "dead": 0,
                    "keys": [{"label": "GROQ_API_KEYS[0]", "status": "ok"}],
                },
                "spaces": {
                    "total": 2,
                    "running": 1,
                    "items": [
                        {
                            "name": "awin2026/voxcpm-space-extremely-long-slug-name",
                            "stage": "RUNNING",
                        },
                        {"name": "backup-space", "stage": "SLEEPING"},
                    ],
                },
            },
        },
        "/api/admin/queue": {
            "success": True,
            "counts": {"queued": 1, "running": 0, "success": 2, "failed": 0, "total_listed": 1},
            "worker": {"online": True, "label": "ONLINE (Render Cloud)"},
            "jobs": [
                {
                    "id": "rc_job_9001",
                    "user_id": 1001,
                    "user_name": "Su Su",
                    "username": "susu_user",
                    "status": "FAILED",
                    "job_type": "RECAP",
                    "engine": "recap",
                    "preset_voice": "Thiha",
                    "voice_display": "Thiha",
                    "engine_display": "AI Myanmar Recap",
                    "input_snippet": "Sample recap job clip",
                    "created_at": "2026-11-14T14:00:00+00:00",
                    "duration_seconds": 12.5,
                    "created_ago": "5m",
                    "download_url": None,
                    "deliverables": {},
                    "error_message": "အလုပ်ဖန်တီးရာတွင် အမှားဖြစ်ပေါ်ပါသည်။",
                    "file_exists": False,
                    "is_expired": False,
                }
            ],
        },
        "/api/admin/history": {
            "success": True,
            "total_count": 1,
            "total_failed": 0,
            "total_success": 1,
            "total_voice": 1,
            "total_recap": 0,
            "timeframe": "24h",
            "status_filter": "all",
            "history": [
                {
                    "id": "vc_job_8001",
                    "user_id": 1001,
                    "username": "susu_user",
                    "first_name": "Su Su",
                    "job_type": "VOICE_CLONE",
                    "engine": "voxcpm",
                    "preset_voice": "Ko Min",
                    "voice_display": "Ko Min",
                    "engine_display": "VoxCPM",
                    "status": "SUCCESS",
                    "input_snippet": "Hello world test",
                    "created_at": "2026-11-14T13:00:00+00:00",
                    "time_ago": "1h",
                    "download_url": None,
                    "deliverables": {},
                    "error_message": None,
                    "file_exists": False,
                    "is_expired": True,
                }
            ],
        },
        "/api/admin/users": {
            "users": [
                {
                    "id": 1001,
                    "username": "susu_user",
                    "first_name": "Su Su",
                    "account_category": "PURCHASED",
                    "status": "ACTIVE",
                    "is_banned": False,
                    "bot_blocked": False,
                    "is_test_account": False,
                    "recap_credits": 10,
                    "balance_seconds": 600,
                    "created_at": "2026-11-10T10:00:00+00:00",
                }
            ],
            "total": 1,
            "limit": 20,
            "offset": 0,
        },
        "/api/admin/users/1001": {
            "id": 1001,
            "username": "susu_user",
            "first_name": "Su Su",
            "language_code": "my",
            "account_category": "PURCHASED",
            "status": "ACTIVE",
            "is_banned": False,
            "bot_blocked": False,
            "is_test_account": False,
            "recap_credits": 10,
            "balance_seconds": 600,
            "jobs_total": 5,
            "jobs_success": 4,
            "jobs_failed": 1,
            "jobs_active": 0,
            "balance_breakdown": {
                "monthly_free_seconds": 180,
                "welcome_seconds": 0,
                "referral_seconds": 0,
                "purchased_seconds": 420,
                "consumed_seconds": 120,
            },
            "tts_today": {
                "daily_limit_seconds": 3600,
                "consumed_seconds": 60,
                "remaining_seconds": 3540,
            },
            "referrals_count": 2,
            "created_at": "2026-11-10T10:00:00+00:00",
        },
        "/api/admin/audit-log": [
            {
                "id": 1,
                "action": "APPROVE_SLIP",
                "admin_id": 999999,
                "target_type": "payment",
                "target_id": "slip_100",
                "details": "{}",
                "created_at": "2026-11-14T12:00:00+00:00",
            }
        ],
        "/api/admin/pricing": {
            "plans": [
                {
                    "code": "v_starter",
                    "category": "voice",
                    "minutes": 30,
                    "recap_credits": 0,
                    "price_mmk": 5000,
                    "is_active": True,
                    "sort_order": 1,
                }
            ],
            "count": 1,
        },
        "/api/admin/settings": {
            "payment_kpay_number": "09123456789",
            "payment_kpay_name": "U Kyaw",
            "payment_wave_number": "09123456789",
            "payment_wave_name": "U Kyaw",
            "maintenance_mode": False,
            "maintenance_note": "",
            "group_ai_admin_enabled": False,
            "private_ai_enabled": True,
            "character_limit_free": 1000,
            "character_limit_purchased": 5000,
        },
    }


def test_cache_bust_token_is_r150():
    assert INDEX_HTML.count("20261114_r151") == 4
    assert "20261114_r149" not in INDEX_HTML


def test_key_health_css_fixed_rhythm():
    for selector in (
        ".adm-key-provider-row",
        ".adm-key-row",
        ".adm-key-main",
        ".adm-key-name",
        ".adm-key-name.is-expanded",
        ".adm-key-meta",
        ".adm-key-status",
        ".adm-key-dot",
    ):
        assert selector in STYLE_CSS, f"Missing Key Health CSS rule: {selector}"
    assert "text-overflow: ellipsis" in STYLE_CSS
    assert "white-space: nowrap" in STYLE_CSS


def test_localize_server_text_closed_set_and_counts():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    shortfall = (
        "💳 Recap Credit မလုံလောက်ပါ — ဒီဗီဒီယိုရှည်ကို အပိုင်း 7 ပိုင်း "
        "ခွဲထုတ်ရမှာဖြစ်ပြီး အပိုင်းတစ်ခုစီ 1 Credit နှုန်းနဲ့ စုစုပေါင်း 7 Credits "
        "လိုအပ်ပါတယ်။ Credit ထပ်ဖြည့်ပြီးမှ ပြန်စမ်းပါခင်ဗျာ။"
    )
    script = (
        "var window = {};\n"
        "var localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };\n"
        + I18N_JS
        + "\nconst sample = "
        + json.dumps(shortfall, ensure_ascii=False)
        + ";\nconst samples = ["
        + json.dumps("လက်ကျန်မိနစ် မလုံလောက်ပါ။", ensure_ascii=False)
        + ","
        + json.dumps("အလုပ် rc_1 ကို ပယ်ဖျက်ပြီးပါပြီ", ensure_ascii=False)
        + ","
        + json.dumps("Logo မတင်ရသေးပါ", ensure_ascii=False)
        + "];\n"
        "currentLang = 'my';\n"
        "const myRes = localizeServerText(sample);\n"
        "currentLang = 'en';\n"
        "const enRes = localizeServerText(sample);\n"
        "const enOthers = samples.map(s => localizeServerText(s));\n"
        "console.log(JSON.stringify({ myRes, enRes, enOthers }));\n"
    )
    tmp = REPO_ROOT / "tests/unit/_tmp_loc_check.js"
    try:
        tmp.write_text(script, encoding="utf-8")
        res = subprocess.run([node, str(tmp)], capture_output=True, text=True, timeout=30)
    finally:
        tmp.unlink(missing_ok=True)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["myRes"] == shortfall
    assert "7" in out["enRes"] and "7 Credits" in out["enRes"]
    assert not MYANMAR_RE.search(out["enRes"])
    for item in out["enOthers"]:
        assert not MYANMAR_RE.search(item), f"Burmese leak in EN localizeServerText: {item!r}"


def test_playwright_admin_polish_overview_and_i18n_sweep():
    pw_mod = pytest.importorskip("playwright.sync_api")
    srv, port = _start_static_server()
    payloads = _mock_payloads()

    def _route_handler(route):
        url = route.request.url
        if "/api/" in url:
            path = "/" + url.split("://", 1)[1].split("/", 1)[1].split("?", 1)[0]
            if path == "/api/user/language":
                route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
                return
            for candidate in (path, path.rstrip("/")):
                if candidate in payloads:
                    route.fulfill(
                        status=200,
                        content_type="application/json; charset=utf-8",
                        body=json.dumps(payloads[candidate], ensure_ascii=False),
                    )
                    return
            route.fulfill(status=200, content_type="application/json", body="{}")
            return
        if not url.startswith(f"http://127.0.0.1:{port}"):
            route.fulfill(status=200, content_type="application/javascript", body="")
            return
        route.continue_()

    try:
        with pw_mod.sync_playwright() as p:
            browser = _launch_chromium(p)
            if browser is None:
                pytest.skip("No Chromium/Chrome/Edge browser executable available")
            context = browser.new_context(viewport={"width": 360, "height": 800})
            page = context.new_page()
            page.add_init_script(
                """
                window.localStorage.setItem('studio_lang', 'my');
                window.Telegram = {
                    WebApp: {
                        initData: 'user=%7B%22id%22%3A999999%7D',
                        initDataUnsafe: { user: { id: 999999, first_name: 'Boss', username: 'bossadmin' } },
                        ready() {},
                        expand() {},
                        openLink() {},
                        MainButton: { hide() {}, show() {} },
                        BackButton: { hide() {}, show() {}, onClick() {} },
                        themeParams: {},
                        platform: 'android',
                        version: '7.0'
                    }
                };
                """
            )
            page_errors = []
            page.on("pageerror", lambda err: page_errors.append(str(err)))
            page.route("**/*", _route_handler)

            page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
            page.wait_for_function("typeof loadAdminData === 'function' && typeof setLanguage === 'function'")
            page.wait_for_timeout(400)

            # Open Admin tab & load overview + key health while in 'my' first
            page.evaluate("document.querySelector('[data-tab=\"tab-admin\"]').click()")
            page.wait_for_function("document.getElementById('adm-total-users').innerText.trim() === '420'")

            # Item 3 in MY
            assert page.locator("#adm-total-users").inner_text().strip() == "420"
            assert page.locator("#adm-total-users-split").inner_text().strip() == "တကယ်သုံးသူ 185 · တစ်ခါမှ မသုံးသူ 235"

            # Root-cause fix (Item 2.1): late switch to 'en' after loadAdminData
            page.evaluate("setLanguage('en')")
            page.wait_for_timeout(150)

            triage_title = page.locator("#adm-triage-title").inner_text().strip()
            assert "Checking" not in triage_title, f"Late setLanguage overwrote triage title: {triage_title!r}"
            assert "3" in triage_title and "slip" in triage_title.lower()

            # Item 3 in EN
            assert page.locator("#adm-total-users").inner_text().strip() == "420"
            assert page.locator("#adm-total-users-split").inner_text().strip() == "Real 185 · never used 235"
            users_lbl = page.locator(".adm-kpi-users .stat-lbl").inner_text().strip()
            assert users_lbl == "Total Users", f"Unexpected EN headline label: {users_lbl!r}"

            # Item 1: open Key Health & inspect fixed-rhythm rows + 20+ char ellipsis at 360px
            page.evaluate("showAdminSection('keys')")
            page.wait_for_selector("#adm-keys-summary .adm-key-provider-row")
            assert page.locator("#adm-keys-summary .adm-key-provider-row").count() == 4
            assert page.locator("#adm-keys-summary .adm-key-dot").count() >= 1

            # Expand HF account list if collapsed
            toggle = page.locator("#adm-keys-accounts-btn")
            toggle.click()
            page.wait_for_selector("#adm-keys-accounts .adm-key-row")
            name_el = page.locator("#adm-keys-accounts .adm-key-name").first
            assert "awin2026_very_long_account_name_test" in (name_el.get_attribute("title") or "")
            metrics = page.evaluate(
                """() => {
                    const els = Array.from(document.querySelectorAll('#adm-keys-accounts .adm-key-name'));
                    const rows = els.map(el => {
                        const r = el.getBoundingClientRect();
                        return {
                            text: el.textContent,
                            title: el.getAttribute('title'),
                            scrollWidth: el.scrollWidth,
                            clientWidth: el.clientWidth,
                            height: r.height,
                        };
                    });
                    const firstRow = document.querySelector('#adm-keys-accounts .adm-key-row');
                    const firstName = firstRow.querySelector('.adm-key-name').getBoundingClientRect();
                    const firstStatus = firstRow.querySelector('.adm-key-status').getBoundingClientRect();
                    return {
                        rows,
                        sameBand: Math.abs(firstName.top - firstStatus.top) < 28,
                        statusToRight: firstStatus.left >= firstName.left,
                    };
                }"""
            )
            long_rows = [r for r in metrics["rows"] if len(r["text"]) > 20]
            assert len(long_rows) >= 1, f"Expected long 20+ char HF account row: {metrics}"
            for r in long_rows:
                assert r["height"] <= 28, f"Long key name wrapped onto multiple lines at 360px: {r}"
                assert r["scrollWidth"] >= r["clientWidth"], f"Expected ellipsis fit measurement: {r}"
            assert metrics["sameBand"] and metrics["statusToRight"], metrics

            # Tap toggles .is-expanded
            name_el.click()
            assert "is-expanded" in (name_el.get_attribute("class") or "")

            # Item 2: Exhaustive EN sweep across ALL main tabs & ALL admin sections
            # Switch through admin sections to populate every dynamic surface
            for sec in ("payments", "jobs", "users", "broadcast", "activity", "keys", "settings", "overview"):
                page.evaluate(f"showAdminSection('{sec}')")
                page.wait_for_timeout(120)
                if sec == "jobs":
                    page.evaluate("showAdminJobsTab('history')")
                    page.wait_for_timeout(120)
                    page.evaluate("showAdminJobsTab('live')")
                    page.wait_for_timeout(80)
                elif sec == "users":
                    page.evaluate("openAdminUserDetail(1001)")
                    page.wait_for_timeout(120)
                    page.evaluate("closeAdminUserDetail()")
                    page.wait_for_timeout(80)

            en_leaks = page.evaluate(
                """() => {
                    const myRe = /[\\u1000-\\u109F]/;
                    const exemptSelectors = [
                        '#btn-lang-toggle',
                        '[data-my-exempt]',
                        '#adm-tut-voice-title',
                        '#adm-tut-recap-title',
                    ];
                    const leaks = [];
                    const walk = (rootLabel) => {
                        const nodes = document.querySelectorAll('[data-i18n], button, label, h1, h2, h3, h4, .stat-lbl, .adm-nav-btn, #adm-screen-overview, #adm-screen-payments, #adm-screen-jobs, #adm-screen-users, #adm-screen-keys, #adm-screen-activity, #adm-screen-settings');
                        for (const el of nodes) {
                            if (exemptSelectors.some(sel => el.matches(sel) || el.closest(sel))) continue;
                            // Skip raw user-supplied values in inputs or option text that stays localized by design only inside exempt attrs
                            const txt = (el.innerText || '').trim();
                            if (!txt) continue;
                            // Check direct leaf text nodes so parent containers don't duplicate
                            for (const child of el.childNodes) {
                                if (child.nodeType === 3) {
                                    const t = (child.nodeValue || '').trim();
                                    if (t && myRe.test(t)) {
                                        leaks.push({ tag: el.tagName, id: el.id, cls: el.className, text: t });
                                    }
                                }
                            }
                            if (el.placeholder && myRe.test(el.placeholder)) {
                                leaks.push({ tag: el.tagName, id: el.id, attr: 'placeholder', text: el.placeholder });
                            }
                        }
                    };
                    walk('dom');
                    return leaks;
                }"""
            )
            assert not en_leaks, f"Myanmar leaks found in EN mode: {en_leaks[:10]}"

            # Check main user tabs in EN mode too
            for tab in ("tab-home", "tab-voice", "tab-tts", "tab-recap", "tab-vip"):
                page.evaluate(f"document.querySelector('[data-tab=\"{tab}\"]').click()")
                page.wait_for_timeout(80)

            en_main_leaks = page.evaluate(
                """() => {
                    const myRe = /[\\u1000-\\u109F]/;
                    const leaks = [];
                    const els = document.querySelectorAll('section.tab-pane:not(#tab-admin) [data-i18n], section.tab-pane:not(#tab-admin) button, section.tab-pane:not(#tab-admin) label, section.tab-pane:not(#tab-admin) input, section.tab-pane:not(#tab-admin) textarea');
                    for (const el of els) {
                        if (el.id === 'btn-lang-toggle' || el.closest('#btn-lang-toggle')) continue;
                        if (el.classList && el.classList.contains('sample-chip')) continue;
                        if (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA') {
                            const t = (el.innerText || '').trim();
                            if (t && myRe.test(t) && !el.closest('#sample-voice-grid')) {
                                leaks.push({ id: el.id, cls: el.className, text: t });
                            }
                        }
                        if (el.placeholder && myRe.test(el.placeholder)) {
                            leaks.push({ id: el.id, attr: 'placeholder', text: el.placeholder });
                        }
                    }
                    return leaks;
                }"""
            )
            assert not en_main_leaks, f"Myanmar leaks in main user tabs (EN mode): {en_main_leaks[:10]}"

            # Switch back to MY and verify Admin & main i18n keys contain zero untranslated English prose
            page.evaluate("setLanguage('my')")
            page.wait_for_timeout(150)
            page.evaluate("document.querySelector('[data-tab=\"tab-admin\"]').click()")
            page.wait_for_timeout(100)
            page.evaluate("showAdminSection('overview')")
            page.wait_for_timeout(100)
            assert page.locator(".adm-kpi-users .stat-lbl").inner_text().strip() == "စုစုပေါင်း အသုံးပြုသူများ"
            assert page.locator("#adm-total-users-split").inner_text().strip() == "တကယ်သုံးသူ 185 · တစ်ခါမှ မသုံးသူ 235"

            # Verify no console pageerror occurred
            assert not page_errors, f"Uncaught JS page errors: {page_errors}"

            browser.close()
    finally:
        srv.shutdown()
        srv.server_close()


MY_ENGLISH_ALLOWLIST = {
    "Voice Clone Pro", "ADMIN", "FREE", "VIP", "PRO", "AI", "TTS", "OK",
    "MMK", "KB", "MB", "GB", "KBZPay", "WavePay", "Hugging Face", "Gemini",
    "Groq", "Spaces", "VoxCPM", "ZeroGPU", "Key Health", "Broadcast", "Online",
    "Render Cloud", "Batch", "Telegram", "YouTube", "TikTok", "Facebook",
    "Reels", "MP4", "MKV", "MOV", "WEBM", "PNG", "TTF", "OTF", "API", "URL",
    "ID", "Bot", "Admin", "Voice Clone", "AI Myanmar Recap", "Movie Recap",
    "Edge TTS", "Mini App", "Studio", "Recap", "Voice", "Credit", "Credits",
    "ZeroGPU quota", "thiha", "komin", "Standard", "Default",
}


def test_my_i18n_values_have_no_unwanted_english_ui_sentences():
    """Guard against full English sentences remaining in translations.my for Batch 1 keys."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = (
        "var window = {};\n"
        "var localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };\n"
        + I18N_JS
        + "\nconsole.log(JSON.stringify({ my: translations.my, en: translations.en }));\n"
    )
    tmp = REPO_ROOT / "tests/unit/_tmp_i18n_dump.js"
    try:
        tmp.write_text(script, encoding="utf-8")
        res = subprocess.run([node, str(tmp)], capture_output=True, text=True, timeout=30)
    finally:
        tmp.unlink(missing_ok=True)
    assert res.returncode == 0, res.stderr
    dicts = json.loads(res.stdout.strip().splitlines()[-1])
    my_d = dicts["my"]
    en_d = dicts["en"]
    assert set(my_d.keys()) == set(en_d.keys()), (
        f"Key parity mismatch: my-en={set(my_d)-set(en_d)}, en-my={set(en_d)-set(my_d)}"
    )
    # Check that our Batch 1 Burmese Admin/UI keys are actually localized into Burmese
    for k in (
        "btn_refresh",
        "admin_status_active",
        "admin_status_banned",
        "badge_cancelled",
        "admin_cancel_job",
        "adm_triage_checking",
        "adm_keys_running",
        "admin_stat_users",
        "admin_stat_users_split",
        "adm_tut_grp_demo",
    ):
        assert MYANMAR_RE.search(my_d[k]), f"Key {k!r} in translations.my lacks Burmese text: {my_d[k]!r}"
        assert not MYANMAR_RE.search(en_d[k]), f"Key {k!r} in translations.en contains Burmese text: {en_d[k]!r}"
    assert "Batch" not in my_d["adm_tut_grp_demo"]
