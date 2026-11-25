"""Unit tests for Part D — Admin Pending Daily Allowance Marker ("မပေးရသေး") (2026-11-25).

Pins:
1. `daily_free_pending_seconds` = 180 for a FREE user with `last_daily_grant_date` = yesterday
   (and when `MinuteBalance` row is missing); = 0 when already granted today, when PURCHASED,
   when the user is an admin, and when `free_daily_allowance_seconds == 0`.
2. Both admin payloads carry `daily_free_pending_seconds`:
   - List/search row dict (`_format_admin_user_dict` / `list_admin_users` / `search_admin_users`)
   - Detail `GET /admin/users/{user_id}` (`balance_breakdown["daily_free_pending_seconds"]`)
   while `balance_seconds` and `daily_free_seconds` remain the true effective values.
3. Strict read-only invariant: calling admin list, search, and detail builders for an
   un-granted user creates NO `DailyFreeAllowanceGrant` row and leaves `MinuteBalance`
   byte-identical.
4. Frontend JS renderers (`admUserRowHtml` and `renderAdminDetailExtras`) display
   `မပေးရသေး` when pending > 0 (`0m 0s · 3m 0s (မပေးရသေး)` on the `နေ့စဉ် အခမဲ့` row)
   and omit it when pending == 0.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models.enums import AccountCategory
from app.models.schema import DailyFreeAllowanceGrant, MinuteBalance, User
from app.services import ledger as ledger_mod
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_admin_daily_free_pending_projection_matrix_and_read_only_invariant(
    db_session, monkeypatch
):
    monkeypatch.setattr(ledger_mod, "get_current_daily_period", lambda: "2026-11-26")
    monkeypatch.setattr(settings, "free_daily_allowance_seconds", 180)
    monkeypatch.setattr(settings, "admin_ids_raw", "999001")

    admin_caller = User(id=999001, username="owner_admin", first_name="Owner", account_category="FREE")
    u_yesterday = User(id=700001, username="free_yday", first_name="Su", account_category=AccountCategory.FREE.value)
    u_missing_bal = User(id=700002, username="free_nobal", first_name="Ko", account_category=AccountCategory.FREE.value)
    u_today = User(id=700003, username="free_today", first_name="Mg", account_category=AccountCategory.FREE.value)
    u_vip = User(id=700004, username="vip_user", first_name="Vip", account_category=AccountCategory.PURCHASED.value)

    db_session.add_all([admin_caller, u_yesterday, u_missing_bal, u_today, u_vip])
    await db_session.flush()

    bal_yesterday = MinuteBalance(
        user_id=u_yesterday.id,
        daily_free_seconds=180,
        last_daily_grant_date="2026-11-25",
    )
    bal_today = MinuteBalance(
        user_id=u_today.id,
        daily_free_seconds=180,
        last_daily_grant_date="2026-11-26",
    )
    bal_vip = MinuteBalance(
        user_id=u_vip.id,
        purchased_seconds=600,
        last_daily_grant_date="2026-11-25",
    )
    db_session.add_all([bal_yesterday, bal_today, bal_vip])
    await db_session.commit()
    await db_session.refresh(bal_yesterday)

    before_snap = (
        bal_yesterday.daily_free_seconds,
        bal_yesterday.last_daily_grant_date,
        bal_yesterday.consumed_seconds,
        bal_yesterday.reserved_daily_free_seconds,
        bal_yesterday.updated_at,
    )

    # 1. List endpoint payload check
    list_res = await api_mod.list_admin_users(limit=30, offset=0, admin=admin_caller, db=db_session)
    by_id = {row["id"]: row for row in list_res["users"]}

    assert by_id[u_yesterday.id]["balance_seconds"] == 0
    assert by_id[u_yesterday.id]["daily_free_pending_seconds"] == 180

    assert by_id[u_missing_bal.id]["balance_seconds"] == 0
    assert by_id[u_missing_bal.id]["daily_free_pending_seconds"] == 180

    assert by_id[u_today.id]["balance_seconds"] == 180
    assert by_id[u_today.id]["daily_free_pending_seconds"] == 0

    assert by_id[u_vip.id]["balance_seconds"] == 600
    assert by_id[u_vip.id]["daily_free_pending_seconds"] == 0

    assert by_id[admin_caller.id]["daily_free_pending_seconds"] == 0

    # 2. Search endpoint payload check
    search_res = await api_mod.search_admin_users(q="free_yday", admin=admin_caller, db=db_session)
    assert len(search_res["users"]) == 1
    assert search_res["users"][0]["daily_free_pending_seconds"] == 180
    assert search_res["users"][0]["balance_seconds"] == 0

    # 3. Detail endpoint payload check (un-granted vs granted)
    detail_yday = await api_mod.get_admin_user_detail(u_yesterday.id, admin=admin_caller, db=db_session)
    assert detail_yday["balance_seconds"] == 0
    assert detail_yday["daily_free_pending_seconds"] == 180
    assert detail_yday["balance_breakdown"]["daily_free_seconds"] == 0
    assert detail_yday["balance_breakdown"]["daily_free_pending_seconds"] == 180

    detail_today = await api_mod.get_admin_user_detail(u_today.id, admin=admin_caller, db=db_session)
    assert detail_today["balance_breakdown"]["daily_free_seconds"] == 180
    assert detail_today["balance_breakdown"]["daily_free_pending_seconds"] == 0

    # 4. Allowance setting = 0 -> pending = 0
    monkeypatch.setattr(settings, "free_daily_allowance_seconds", 0)
    detail_zero_cfg = await api_mod.get_admin_user_detail(u_yesterday.id, admin=admin_caller, db=db_session)
    assert detail_zero_cfg["daily_free_pending_seconds"] == 0
    assert detail_zero_cfg["balance_breakdown"]["daily_free_pending_seconds"] == 0

    # 5. Strict read-only invariant: zero DailyFreeAllowanceGrant rows created, MinuteBalance untouched
    grant_cnt = (
        await db_session.execute(select(func.count(DailyFreeAllowanceGrant.id)))
    ).scalar_one()
    assert grant_cnt == 0

    await db_session.refresh(bal_yesterday)
    after_snap = (
        bal_yesterday.daily_free_seconds,
        bal_yesterday.last_daily_grant_date,
        bal_yesterday.consumed_seconds,
        bal_yesterday.reserved_daily_free_seconds,
        bal_yesterday.updated_at,
    )
    assert after_snap == before_snap
    assert await db_session.get(MinuteBalance, u_missing_bal.id) is None


def test_admin_frontend_renders_pending_marker_in_list_and_detail_node(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")

    row_start = APP_JS.index("function admUserRowHtml(u)")
    row_end = APP_JS.index("let admUsersCategory =", row_start)
    row_fn = APP_JS[row_start:row_end]

    fmt_start = APP_JS.index("function admFmtSecs(total)")
    det_end = APP_JS.index("function renderAdminBanZone", fmt_start) if "function renderAdminBanZone" in APP_JS[fmt_start:] else APP_JS.index("async function adminSendUserMessage", fmt_start)
    det_fn = APP_JS[fmt_start:det_end]

    script = (
        "var window = {};\n"
        "var localStorage = { getItem: () => 'my', setItem: () => {}, removeItem: () => {} };\n"
        "const els = {};\n"
        "function makeEl() { return { innerHTML: '', innerText: '', classList: { add(){}, remove(){}, toggle(){} } }; }\n"
        "for (const id of ['adm-detail-breakdown','adm-detail-grants','adm-detail-tts','adm-detail-gemini-key','adm-detail-verify','adm-detail-referrals','adm-detail-payments','adm-detail-custom-voices','adm-detail-private-ai']) els[id] = makeEl();\n"
        "var document = { getElementById: (id) => els[id] || null };\n"
        "function escapeHtml(s) { return String(s ?? ''); }\n"
        "function renderGrantsRowsHtml() { return ''; }\n"
        "function admFmtDate() { return '-'; }\n"
        + I18N_JS + "\n" + row_fn + "\n" + det_fn + "\n"
        "currentLang = 'my';\n"
        "const rowPending = admUserRowHtml({ id: 1, first_name: 'Aung', account_category: 'FREE', balance_seconds: 0, daily_free_pending_seconds: 180, recap_credits: 0 });\n"
        "const rowGranted = admUserRowHtml({ id: 2, first_name: 'Mya', account_category: 'FREE', balance_seconds: 180, daily_free_pending_seconds: 0, recap_credits: 0 });\n"
        "renderAdminDetailExtras({ balance_breakdown: { daily_free_seconds: 0, daily_free_pending_seconds: 180, monthly_free_seconds: 0, welcome_seconds: 0, referral_voice_seconds: 0, referral_seconds: 0, purchased_seconds: 0, consumed_seconds: 0 } });\n"
        "const bdPending = els['adm-detail-breakdown'].innerHTML;\n"
        "renderAdminDetailExtras({ balance_breakdown: { daily_free_seconds: 180, daily_free_pending_seconds: 0, monthly_free_seconds: 0, welcome_seconds: 0, referral_voice_seconds: 0, referral_seconds: 0, purchased_seconds: 0, consumed_seconds: 0 } });\n"
        "const bdGranted = els['adm-detail-breakdown'].innerHTML;\n"
        "console.log(JSON.stringify({ rowPending, rowGranted, bdPending, bdGranted }));\n"
    )
    tmp_js = tmp_path / "_adm_pending_test.js"
    tmp_js.write_text(script, encoding="utf-8")
    res = subprocess.run([node, str(tmp_js)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])

    assert "0m" in out["rowPending"]
    assert "မပေးရသေး" in out["rowPending"]
    assert "3m" in out["rowGranted"]
    assert "မပေးရသေး" not in out["rowGranted"]

    assert "နေ့စဉ် အခမဲ့" in out["bdPending"]
    assert "0m 0s · 3m 0s (မပေးရသေး)" in out["bdPending"]
    assert "3m 0s" in out["bdGranted"]
    assert "မပေးရသေး" not in out["bdGranted"]
