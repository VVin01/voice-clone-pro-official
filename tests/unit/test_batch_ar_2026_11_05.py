"""Batch AR — maintenance mode blocks Mini App ENTRY for non-admins.

Owner order (Ulfa, 2026-11-05 20:40): "maintenance ဖွင့်ထားချိန်မှာ
mini app ပါဝင်မရအောင်လုပ်ချင်တယ်" — while maintenance is ON, a
non-admin must not be able to enter the Mini App AT ALL.

Before AR, maintenance only refused WRITES (hunt C1, inside
require_gate_verified); reads stayed open, so users could open,
browse and read everything and only failed when they tried to act.
AR extends the SAME mechanism to the whole authenticated surface:

- get_current_user() — the identity dependency behind every Mini App
  endpoint — raises the shared MAINTENANCE_MODE 403 for non-admins
  while the flag is on (admins exempt, same as the write guard);
- get_optional_user() re-raises that refusal instead of anonymising
  it into the public preview;
- the signed-token file endpoints (recap download/stream, user logo)
  re-check the helper after resolving a token's user;
- GET /api/config (unauthenticated — the gate's own lifeline) now
  also carries maintenance_note, and the client renders a dedicated
  "maintenance" entry-gate mode (title + note, no join UI) that is
  decided BEFORE any cached-profile unlock.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from fastapi import HTTPException

import app.webapp.api as api_mod
from app.config import settings
from app.models.schema import User
from app.services.ledger import get_or_create_user

STATIC_DIR = Path(api_mod.__file__).resolve().parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC_DIR / "js" / "i18n.js").read_text(encoding="utf-8")

ADMIN_ID = 999333


def _forge_init_data(monkeypatch, user_id: int):
    """Make get_current_user resolve the given Telegram id."""
    monkeypatch.setattr(
        api_mod,
        "parse_and_verify_init_data",
        lambda data: {"id": user_id, "first_name": "T", "language_code": "my"},
    )


async def _make_user(db_session, uid: int) -> User:
    user, _ = await get_or_create_user(db_session, telegram_id=uid)
    await db_session.commit()
    return user


# ── Server: the identity dependency refuses reads during maintenance ──


@pytest.mark.asyncio
async def test_ar_current_user_blocked_for_non_admin_during_maintenance(db_session, monkeypatch):
    await _make_user(db_session, 8810)
    _forge_init_data(monkeypatch, 8810)
    monkeypatch.setattr(settings, "maintenance_mode", True)
    with pytest.raises(HTTPException) as exc:
        await api_mod.get_current_user("init-data", db_session)
    assert exc.value.status_code == 403
    assert exc.value.detail.startswith("MAINTENANCE_MODE")
    # The refusal carries the SHARED detail string (one error shape).
    assert exc.value.detail == api_mod.MAINTENANCE_MODE_DETAIL


@pytest.mark.asyncio
async def test_ar_current_user_admin_exempt_during_maintenance(db_session, monkeypatch):
    await _make_user(db_session, ADMIN_ID)
    _forge_init_data(monkeypatch, ADMIN_ID)
    monkeypatch.setattr(settings, "maintenance_mode", True)
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    user = await api_mod.get_current_user("init-data", db_session)
    assert user.id == ADMIN_ID


@pytest.mark.asyncio
async def test_ar_current_user_open_when_maintenance_off(db_session, monkeypatch):
    await _make_user(db_session, 8811)
    _forge_init_data(monkeypatch, 8811)
    monkeypatch.setattr(settings, "maintenance_mode", False)
    user = await api_mod.get_current_user("init-data", db_session)
    assert user.id == 8811


@pytest.mark.asyncio
async def test_ar_write_guard_keeps_the_same_refusal(db_session, monkeypatch):
    # Regression: the hunt-C1 write guard still refuses with the
    # identical shape (the constant refactor changed nothing).
    user = await _make_user(db_session, 8812)
    monkeypatch.setattr(settings, "maintenance_mode", True)
    with pytest.raises(HTTPException) as exc:
        await api_mod.require_gate_verified(user=user, db=db_session)
    assert exc.value.status_code == 403
    assert exc.value.detail == api_mod.MAINTENANCE_MODE_DETAIL


# ── Server: the helper's own matrix ───────────────────────────────────


@pytest.mark.asyncio
async def test_ar_enforce_helper_matrix(db_session, monkeypatch):
    user = await _make_user(db_session, 8813)
    admin = await _make_user(db_session, ADMIN_ID)
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))

    monkeypatch.setattr(settings, "maintenance_mode", True)
    with pytest.raises(HTTPException) as exc:
        api_mod._enforce_maintenance_access(user)
    assert exc.value.status_code == 403
    api_mod._enforce_maintenance_access(admin)  # admin exempt: no raise

    monkeypatch.setattr(settings, "maintenance_mode", False)
    api_mod._enforce_maintenance_access(user)  # flag off: no raise


# ── Server: optional-user endpoints must not anonymise the refusal ────


@pytest.mark.asyncio
async def test_ar_optional_user_reraises_maintenance(db_session, monkeypatch):
    await _make_user(db_session, 8814)
    _forge_init_data(monkeypatch, 8814)
    monkeypatch.setattr(settings, "maintenance_mode", True)
    with pytest.raises(HTTPException) as exc:
        await api_mod.get_optional_user("init-data", db_session)
    assert exc.value.status_code == 403
    assert exc.value.detail.startswith("MAINTENANCE_MODE")


@pytest.mark.asyncio
async def test_ar_optional_user_anonymous_preview_preserved(db_session, monkeypatch):
    # No initData at all -> anonymous preview (None), even during
    # maintenance: the refusal targets IDENTIFIED non-admins.
    monkeypatch.setattr(settings, "maintenance_mode", True)
    assert await api_mod.get_optional_user(None, db_session) is None


@pytest.mark.asyncio
async def test_ar_optional_user_normal_when_off(db_session, monkeypatch):
    await _make_user(db_session, 8815)
    _forge_init_data(monkeypatch, 8815)
    monkeypatch.setattr(settings, "maintenance_mode", False)
    user = await api_mod.get_optional_user("init-data", db_session)
    assert user is not None and user.id == 8815


# ── Server: token-resolved file endpoints re-check maintenance ────────


def test_ar_token_file_endpoints_recheck_maintenance():
    # Both endpoints resolve a user from a signed token WITHOUT
    # get_current_user(); the helper must be re-checked in-body,
    # after the AJ L11 ban check (same placement discipline).
    for fn_name in ("download_recap_file", "get_user_logo_file"):
        src = inspect.getsource(getattr(api_mod, fn_name))
        assert "_enforce_maintenance_access(user)" in src, fn_name
    src = inspect.getsource(api_mod.download_recap_file)
    assert src.index("BLOCKED") < src.index("_enforce_maintenance_access(user)")


def test_ar_get_current_user_enforces_before_return():
    src = inspect.getsource(api_mod.get_current_user)
    assert "_enforce_maintenance_access(user)" in src
    assert src.index("_enforce_maintenance_access(user)") < src.index("return user")


# ── Server: /api/config carries what the gate needs ───────────────────


@pytest.mark.asyncio
async def test_ar_public_config_carries_maintenance_note(db_session, monkeypatch):
    monkeypatch.setattr(settings, "maintenance_mode", True)
    monkeypatch.setattr(settings, "maintenance_note", "ည ၉:၃၀ ပြန်ဖွင့်ပါမယ်")
    cfg = await api_mod.get_public_app_config(db_session)
    assert cfg["maintenance_mode"] is True
    assert cfg["maintenance_note"] == "ည ၉:၃၀ ပြန်ဖွင့်ပါမယ်"

    monkeypatch.setattr(settings, "maintenance_note", "")
    cfg = await api_mod.get_public_app_config(db_session)
    assert cfg["maintenance_note"] == ""


# ── Client: the maintenance gate is the initial, blocking state ───────


def test_ar_client_gate_has_maintenance_mode():
    # setEntryGateMode grows a dedicated maintenance branch rendering
    # the note (or the default text) and hiding the join UI.
    assert 'mode === "maintenance"' in APP_JS
    assert 't("maintenance_title")' in APP_JS
    assert "data.maintenance_note" in APP_JS
    assert 't("maintenance_default_desc")' in APP_JS
    # enterMaintenanceLock exists, is exported for the interceptor,
    # and re-reads the note from the one endpoint that stays open.
    assert "async function enterMaintenanceLock()" in APP_JS
    assert "window.enterMaintenanceLock = enterMaintenanceLock" in APP_JS
    assert 'fetchWithTimeout("/api/config"' in APP_JS


def test_ar_client_boot_checks_maintenance_before_cached_unlock():
    src = APP_JS[APP_JS.index("async function initEntryGate()"):]
    src = src[: src.index("runEntryGateCheck(\"startup\")")]
    # The /api/config maintenance decision precedes — and can
    # pre-empt — the cached-profile unlock, so the app never flashes.
    assert 'cfg.maintenance_mode' in src
    assert src.index('"/api/config"') < src.index("bootFromCachedProfile()")
    assert 'setEntryGateMode("maintenance", maintenanceCfg)' in src


def test_ar_client_mid_session_refusals_swap_to_maintenance():
    # The gate check itself maps a MAINTENANCE_MODE 403 to the
    # maintenance screen, and so does the generic fetch interceptor
    # (maintenance switched on while the app was open).
    assert 'String(errData.detail || "").includes("MAINTENANCE_MODE")' in APP_JS
    assert 'detail.includes("MAINTENANCE_MODE")' in APP_JS
    assert "enterMaintenanceLock()" in APP_JS


def test_ar_client_gate_markup_and_i18n():
    # The lock card's emoji is addressable so the maintenance mode
    # can swap 🔐 for 🛠️, and both locales carry the gate strings.
    assert 'id="gate-lock-emoji"' in INDEX_HTML
    assert I18N_JS.count("maintenance_title:") == 2
    assert I18N_JS.count("maintenance_default_desc:") == 2


def test_ar_cache_token_r66():
    assert INDEX_HTML.count("?v=20261104_r84") == 4
    assert "20261104_r65" not in INDEX_HTML
