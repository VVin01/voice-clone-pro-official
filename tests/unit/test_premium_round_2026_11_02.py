"""Premium-round fix batch (2026-11-02) — Release 37 pins.

Built from premium-round-2026-11-02/REPORT.md (live inspection,
three personas, read-only):

- D1 (High): the bot-chat /admin dashboard crashed for EVERY admin —
  Postgres SUM() over a NUMERIC-backed MinuteBalance column returns
  decimal.Decimal and ``Decimal / 60.0`` raises TypeError
  (traceback_admin_085023.txt). SQLite tests returned ints, so the
  suite stayed green while prod was broken. Both sums are now
  coerced to int at the source; the regression pins below feed
  Decimal sums through the real dashboard path.
- D2: R34's fitBlurPlateToCaption() only ran on slider syncs; after
  Analyze the preview wrap un-hides AFTER the last sync, so the fit
  measured a hidden (0-height) plate and never engaged — the caption
  spilled 4.8px top+bottom. The fit now re-runs at the settle points
  (Analyze completion, upload ready, subtitle style change).
- D3/E1: admin user payloads now expose ``bot_blocked`` and the Mini
  App renders a distinct BOT BLOCKED label, so bot-blocked users are
  no longer indistinguishable from admin bans.
- P1: a failed/timed-out Downloader extract now leaves a PERSISTENT
  honest failure box instead of settling silently to idle.
- P2: the History relative-time phrase ("45h ago") is a nowrap
  token — it no longer splits across lines on badge-carrying cards.
- P3: the referral link wraps with balanced lines (no orphan char).
- W1: RS-3 metric clip — the four single-line name/title spans get
  the app's Myanmar line-height standard (1.8).
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.models.schema import User
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
CSS = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
APP_PY = (ROOT / "app" / "handlers" / "admin.py").read_text(encoding="utf-8")


def _js_slice(start_marker: str, end_marker: str) -> str:
    i = APP_JS.index(start_marker)
    j = APP_JS.index(end_marker, i + len(start_marker))
    return APP_JS[i:j]


def _css_rule(selector: str) -> str:
    i = CSS.index(selector)
    j = CSS.index("}", i)
    return CSS[i:j]


# ── Statics token ───────────────────────────────────────────────────

def test_statics_token_bumped_to_r37():
    assert HTML.count("?v=20261102_r37") == 3
    assert "20261102_ma1" not in HTML


# ── D1: Decimal dashboard sums ──────────────────────────────────────

class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value


class _DecimalSumSession:
    """Wraps a real session but answers the two MinuteBalance SUM
    queries with Decimals — exactly what the Postgres dialect returns
    for a NUMERIC-backed column (the prod condition)."""

    def __init__(self, real, purchased: Decimal, consumed: Decimal):
        self._real = real
        self._purchased = purchased
        self._consumed = consumed

    async def execute(self, stmt, *args, **kwargs):
        sql = str(stmt).lower()
        if "sum(" in sql and "purchased_seconds" in sql:
            return _ScalarResult(self._purchased)
        if "sum(" in sql and "consumed_seconds" in sql:
            return _ScalarResult(self._consumed)
        return await self._real.execute(stmt, *args, **kwargs)


@pytest.mark.asyncio
async def test_d1_dashboard_metrics_survive_decimal_sums(db_session):
    from app.handlers.admin import _fetch_dashboard_metrics

    wrapped = _DecimalSumSession(
        db_session, purchased=Decimal("3661"), consumed=Decimal("7265")
    )
    data = await _fetch_dashboard_metrics(wrapped)
    # 3661s -> 61.0m ; 7265s -> 121.1m, correctly rounded, no TypeError.
    assert data["purchased_minutes"] == 61.0
    assert data["consumed_minutes"] == 121.1
    assert isinstance(data["purchased_minutes"], float)
    assert isinstance(data["consumed_minutes"], float)


@pytest.mark.asyncio
async def test_d1_dashboard_render_survives_decimal_sums(db_session):
    from app.handlers.admin import render_admin_dashboard

    wrapped = _DecimalSumSession(
        db_session, purchased=Decimal("3661"), consumed=Decimal("7265")
    )
    text, kb = await render_admin_dashboard(
        admin_id=888111, lang="en", session=wrapped
    )
    assert "61.0m" in text
    assert "121.1m" in text
    assert kb is not None


@pytest.mark.asyncio
async def test_d1_dashboard_decimal_zero_sums(db_session):
    from app.handlers.admin import _fetch_dashboard_metrics

    wrapped = _DecimalSumSession(
        db_session, purchased=Decimal("0"), consumed=Decimal("0")
    )
    data = await _fetch_dashboard_metrics(wrapped)
    assert data["purchased_minutes"] == 0.0
    assert data["consumed_minutes"] == 0.0


def test_d1_sweep_dashboard_is_the_only_sql_sum_site():
    """The dialect-gap sweep: func.sum( over app/ exists ONLY in the
    dashboard metrics (fixed above) — every other division site reads
    per-row ORM attributes, which the dialect returns as ints."""
    app_root = ROOT / "app"
    sum_sites = []
    for py in app_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if "func.sum(" in text:
            sum_sites.append(py.relative_to(ROOT).as_posix())
    assert sum_sites == ["app/handlers/admin.py"]
    # …and both of its sum lines are int()-coerced at the source.
    assert "purchased_sec = int((await session.execute(select(func.sum(MinuteBalance.purchased_seconds)))).scalar() or 0)" in APP_PY
    assert "consumed_sec = int((await session.execute(select(func.sum(MinuteBalance.consumed_seconds)))).scalar() or 0)" in APP_PY


# ── D2: plate fit engages at the settle points ──────────────────────

def test_d2_refit_helper_defined():
    body = _js_slice(
        "function refitBlurPlateAfterSettle()",
        "function syncSubBoxVisual()",
    )
    assert "requestAnimationFrame" in body
    assert "fitBlurPlateToCaption()" in body


def test_d2_analyze_completion_refits_plate():
    body = _js_slice(
        "// 14. Analyze / Video Info Check Button",
        "// Auto-detect on input / paste",
    )
    finally_at = body.index("} finally {")
    assert "refitBlurPlateAfterSettle();" in body[finally_at:]


def test_d2_upload_ready_refits_plate():
    body = _js_slice(
        'setRecapInfoUploadState("ready");',
        'if (els.cancelBtn) els.cancelBtn.classList.add("hidden");',
    )
    assert 'setRecapPreviewState("ready");' in body
    assert "refitBlurPlateAfterSettle();" in body


def test_d2_style_chip_change_refits_plate():
    body = _js_slice(
        'const styleChips = document.querySelectorAll("#sub-style-picker .style-chip");',
        "// 10. Subtitle Live Preview Renderer",
    )
    assert "fitBlurPlateToCaption();" in body


# ── D3/E1: bot_blocked provenance in admin payloads + UI ────────────

@pytest.mark.asyncio
async def test_d3_admin_user_row_exposes_bot_blocked(db_session):
    u = User(id=777001, first_name="Flagged", bot_blocked=True)
    db_session.add(u)
    await db_session.flush()
    row = await api_mod._admin_user_row(db_session, u)
    assert row["bot_blocked"] is True
    assert row["is_banned"] is False

    u2 = User(id=777002, first_name="Plain", status="BLOCKED")
    db_session.add(u2)
    await db_session.flush()
    row2 = await api_mod._admin_user_row(db_session, u2)
    assert row2["bot_blocked"] is False
    assert row2["is_banned"] is True


@pytest.mark.asyncio
async def test_d3_admin_users_list_exposes_bot_blocked(db_session):
    admin = User(id=999001, first_name="Admin")
    u = User(id=777003, first_name="Listed", bot_blocked=True)
    db_session.add_all([admin, u])
    await db_session.flush()
    payload = await api_mod.list_admin_users(
        limit=30, offset=0, admin=admin, db=db_session, category="", search=""
    )
    rows = {r["id"]: r for r in payload["users"]}
    assert rows[777003]["bot_blocked"] is True
    assert rows[999001]["bot_blocked"] is False


def test_d3_ui_labels_bot_blocked_distinctly():
    list_body = _js_slice(
        '<span class="badge-status ${u.is_banned',
        "</span>",
    )
    assert "bot_blocked" in list_body
    assert "BOT BLOCKED" in list_body
    detail_body = _js_slice(
        "function renderAdminUserDetail(u)",
        "function renderAdminBanZone(u)",
    )
    assert 't("admin_status_bot_blocked")' in detail_body
    # Burmese label, proofread: "Bot က ပိတ်ထားသည်" (blocked by the bot).
    assert 'admin_status_bot_blocked: "Bot က ပိတ်ထားသည်"' in I18N_JS
    assert 'admin_status_bot_blocked: "BOT BLOCKED"' in I18N_JS
    assert "background: var(--gold-soft)" in _css_rule(".badge-status.botblocked")


# ── P1: Downloader persistent failure state ─────────────────────────

def test_p1_error_box_markup_and_copy():
    assert 'id="dl-error-box"' in HTML
    assert 'data-i18n="dl_extract_failed_title"' in HTML
    assert 'data-i18n="dl_extract_failed_hint"' in HTML
    assert 'dl_extract_failed_title: "ဗီဒီယို အချက်အလက် ထုတ်ယူ၍ မရပါ"' in I18N_JS
    assert 'dl_extract_failed_hint: "လင့်ခ် မှန်မမှန် စစ်ပြီး ထပ်ကြိုးစားပါ' in I18N_JS
    assert 'dl_extract_failed_title: "Couldn\'t extract this video"' in I18N_JS
    assert ".dl-error-box" in CSS


def test_p1_extract_handler_shows_and_clears_error_box():
    body = _js_slice("function setupDownloader()", "const fmtButtons")
    # Failure (incl. client timeout) leaves the persistent box up.
    assert "dlShowExtractError();" in body
    # A new attempt and a success both clear it.
    assert body.count("dlHideExtractError();") >= 2
    helpers = _js_slice("function dlShowExtractError()", "function dlSetSrtCardState")
    assert 'getElementById("dl-error-box")' in helpers


# ── P2: History relative-time never splits ──────────────────────────

def test_p2_history_time_ago_is_nowrap_token():
    body = _js_slice("function hubHistoryRowHtml(j)", "function renderHistoryHub()")
    assert 'class="hub-time-ago"' in body
    assert "white-space: nowrap" in _css_rule(".hub-time-ago")


# ── P3: referral link balanced wrap ─────────────────────────────────

def test_p3_referral_link_balanced_wrap():
    rule = _css_rule(".ref-link-text {\n    display: block;")
    assert "text-wrap: balance" in rule
    assert "word-break: break-all" in rule  # full link stays visible


# ── W1: RS-3 metric clip line-heights ───────────────────────────────

def test_w1_name_spans_use_myanmar_line_height():
    assert "line-height: 1.8" in _css_rule(".voice-card .v-name")
    assert "line-height: 1.8" in _css_rule(".vsr-name")
    assert "line-height: 1.8" in _css_rule(".recap-info-title")
    assert "line-height: 1.8" in _css_rule(".plan-tabs .seg-title")
